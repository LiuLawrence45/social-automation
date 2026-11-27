local Camera = __require("camera")
local Pose = __require("pose")

-- Posture and clipping measured from the posed scene: body parts are oriented boxes, shrunk a little so that
-- touching is not passing through, and every finding says whether the offending part is in the frame.
local M = {}

local BODY = {
	"Head", "UpperTorso", "LowerTorso",
	"LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand",
	"LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "RightUpperLeg", "RightLowerLeg", "RightFoot",
}
local SIDES = { L = "Left", R = "Right" }
local SHRINK = 0.72
local DEPTH = 0.07
local SOFT_DEPTH = 0.5
-- A hand resting on the part of its own body it was sent to (a mouth, a chest) presses against it: the boxes
-- are squarer than a face or a ribcage, so that much overlap is contact and not passing through.
local REST_DEPTH = 0.3
local SOFT = { "pillow", "mattress", "cushion" }
local COVER = { "blanket", "duvet" }
local SUPPORT_MARGIN = 0.14
local REACH_TOLERANCE = 0.4
local ARM_RAISED = 35
local ARM_SPLAYED = 14
local STANCE_WIDE = 11
local NEAR_BODY = 0.75
-- Pairs of one body that may not pass through each other; neighbours on the skeleton are left out.
local SELF_PAIRS = {}
for _, side in pairs(SIDES) do
	local other = side == "Left" and "Right" or "Left"
	for _, limb in ipairs({ side .. "LowerArm", side .. "Hand" }) do
		for _, target in ipairs({ "Head", "UpperTorso", "LowerTorso", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg" }) do
			table.insert(SELF_PAIRS, { limb, target })
		end
		if side == "Left" then
			for _, target in ipairs({ other .. "UpperArm", other .. "LowerArm", other .. "Hand" }) do
				table.insert(SELF_PAIRS, { limb, target })
			end
		else
			table.insert(SELF_PAIRS, { limb, other .. "UpperArm" })
		end
	end
	for _, limb in ipairs({ side .. "LowerLeg", side .. "Foot" }) do
		for _, target in ipairs({ "Head", "UpperTorso" }) do
			table.insert(SELF_PAIRS, { limb, target })
		end
		if side == "Left" then
			for _, target in ipairs({ "RightLowerLeg", "RightFoot" }) do
				table.insert(SELF_PAIRS, { limb, target })
			end
		else
			table.insert(SELF_PAIRS, { limb, "LeftLowerLeg" })
		end
	end
	table.insert(SELF_PAIRS, { side .. "Foot", "LowerTorso" })
	table.insert(SELF_PAIRS, { side .. "UpperArm", "Head" })
end
-- Degrees a joint may turn. Elbows and knees are hinges: bend is the turn about the limb's sideways axis (an elbow
-- folds forwards, a knee backwards), side is how far the limb leaves that plane, twist the turn about its length.
-- A thigh is measured by where it points. The rest are read as nod (x), turn (y) and tilt (z).
local HINGES = {
	LowerArm = { bend = { -8, 152 }, side = 12, twist = 78 },
	LowerLeg = { bend = { -152, 8 }, side = 10, twist = 20 },
}
local LIMITS = {
	Head = { x = { -50, 50 }, y = 85, z = 40 },
	UpperTorso = { x = { -40, 55 }, y = 50, z = 35 },
	Foot = { x = { -55, 55 }, y = 35, z = 35 },
	Hand = { x = { -85, 85 }, y = 35, z = 60 },
}
local THIGH = { forward = { -45, 135 }, out = { -25, 55 } }

local function solid(part)
	return part.Transparency < 0.6 and part.Name:sub(1, 6) ~= "Light_"
end

local function soft(part, words)
	local name = part.Name:lower()
	for _, word in ipairs(words or SOFT) do
		if name:find(word, 1, true) then
			return true
		end
	end
	return false
end

local overlap = Pose.overlap

local function distanceToBox(point, cf, size)
	local p = cf:PointToObjectSpace(point)
	local dx = math.max(math.abs(p.X) - size.X / 2, 0)
	local dy = math.max(math.abs(p.Y) - size.Y / 2, 0)
	local dz = math.max(math.abs(p.Z) - size.Z / 2, 0)
	return math.sqrt(dx * dx + dy * dy + dz * dz)
end

local function project(view, point)
	local frame = CFrame.lookAt(view.pos, view.look) * CFrame.Angles(0, 0, math.rad(view.roll or 0))
	local p = frame:PointToObjectSpace(point)
	local depth = -p.Z
	if depth <= 0.05 then
		return nil
	end
	local half = math.tan(math.rad(view.fov / 2))
	return 0.5 + p.X / depth / (2 * half), 0.5 - p.Y / depth / (2 * half * Camera.ASPECT), depth
end

local function round(value, places)
	local k = 10 ^ (places or 2)
	return math.floor(value * k + 0.5) / k
end

local function angle(a, b)
	return math.deg(math.acos(math.clamp(a.Unit:Dot(b.Unit), -1, 1)))
end

function M.check(stage, index, camera, samples)
	local shot = stage.timeline.shots[index]
	if camera then
		stage.cameras = stage.cameras or {}
		stage.cameras[shot.id] = camera
	end
	stage.current = nil
	local out = {}
	local last = math.max(shot.frame_start, shot.frame_end - 1)
	for _, u in ipairs(samples) do
		local frameIndex = shot.frame_start + math.floor((last - shot.frame_start) * u + 0.5)
		stage:frame(frameIndex)
		local state = stage.current
		local view = state.view
		local scenery = OverlapParams.new()
		scenery.FilterType = Enum.RaycastFilterType.Exclude
		scenery.FilterDescendantsInstances = { stage.castFolder, stage.propFolder }
		local rays = RaycastParams.new()
		rays.FilterType = Enum.RaycastFilterType.Exclude
		rays.FilterDescendantsInstances = { stage.castFolder, stage.propFolder }

		local function seen(point)
			local x, y, depth = project(view, point)
			if not x or x < -0.02 or x > 1.02 or y < -0.02 or y > 1.02 then
				return false
			end
			local direction = point - view.pos
			local skip = { stage.castFolder, stage.propFolder }
			for _ = 1, 6 do
				rays.FilterDescendantsInstances = skip
				local hit = workspace:Raycast(view.pos, direction, rays)
				if not hit then
					return true
				end
				if solid(hit.Instance) then
					return hit.Distance > depth - 0.3
				end
				table.insert(skip, hit.Instance)
			end
			return true
		end

		local bodies = {}
		for who, entry in pairs(state.actors) do
			if entry.avatar and not entry.hidden then
				local parts = {}
				for _, name in ipairs(BODY) do
					local part = entry.model:FindFirstChild(name)
					if part then
						parts[name] = { part = part, cf = part.CFrame, size = part.Size, seen = seen(part.Position) }
					end
				end
				bodies[who] = { entry = entry, parts = parts, scale = entry.scale or 1 }
			end
		end

		local actors = {}
		for who, body in pairs(bodies) do
			local entry, parts, scale = body.entry, body.parts, body.scale
			local issues, metrics = {}, {}
			local function issue(code, detail, visible, depth)
				table.insert(issues, { code = code, detail = detail, seen = visible and true or false, depth = depth and round(depth) or nil })
			end
			local anySeen = false
			for _, name in ipairs({ "Head", "UpperTorso", "LowerTorso", "LeftHand", "RightHand", "LeftFoot", "RightFoot" }) do
				if parts[name] and parts[name].seen then
					anySeen = true
				end
			end
			metrics.seen = anySeen

			-- A hand that holds a prop or has a named target is busy; what it may touch is not clipping.
			local busy, touching, resting = {}, {}, {}
			for _, hold in ipairs(entry.holds or {}) do
				busy[hold.hand or "R"] = "prop"
			end
			for side in pairs(entry.wants_prop or {}) do
				if not busy[side] then
					issue("prop_missing", side .. " hand is posed to hold something and holds nothing", anySeen)
				end
			end
			for side, reach in pairs(entry.reach or {}) do
				busy[side] = busy[side] or "target"
				local hand = parts[SIDES[side] .. "Hand"]
				if hand and reach.point and reach.active ~= false then
					local off = (hand.cf.Position - reach.point).Magnitude
					if off > (reach.tolerance or REACH_TOLERANCE) * scale then
						issue("hand_off_target", side .. ":" .. tostring(reach.name), hand.seen, off)
					end
				end
				if reach.part then
					resting[SIDES[side] .. "Hand->" .. reach.part] = true
					resting[SIDES[side] .. "LowerArm->" .. reach.part] = true
				end
				if reach.touch then
					touching[SIDES[side] .. "Hand"] = true
					touching[SIDES[side] .. "LowerArm"] = reach.touch == "arm" or nil
				end
			end

			-- Scenery: walls, doors, furniture, the floor.
			local supported = false
			for _, name in ipairs(BODY) do
				local item = parts[name]
				if item then
					local found = workspace:GetPartBoundsInBox(item.cf, item.size + Vector3.one * (2 * SUPPORT_MARGIN), scenery)
					for _, other in ipairs(found) do
						if solid(other) then
							if overlap(item.cf, item.size + Vector3.one * (2 * SUPPORT_MARGIN), other.CFrame, other.Size) > 0 then
								supported = true
							end
							if not touching[name] and not soft(other, COVER) then
								local depth = overlap(item.cf, item.size * SHRINK, other.CFrame, other.Size)
								local limit = (soft(other) and SOFT_DEPTH or DEPTH) * scale
								local flatCover = other.Size.Y < 0.16 and math.abs(other.CFrame.UpVector.Y) > 0.95
								if depth > limit and not flatCover then
									issue("clip_scenery", name .. "->" .. other.Name, item.seen, depth)
								end
							end
						end
					end
				end
			end
			if not supported then
				issue("floating", "nothing under or against the body", anySeen)
			end

			-- The body through itself.
			for _, pair in ipairs(SELF_PAIRS) do
				local a, b = parts[pair[1]], parts[pair[2]]
				if a and b then
					local depth = overlap(a.cf, a.size * SHRINK * (Pose.TRIM[pair[1]] or Vector3.one), b.cf, b.size * SHRINK * (Pose.TRIM[pair[2]] or Vector3.one))
					local limit = resting[pair[1] .. "->" .. pair[2]] and REST_DEPTH or DEPTH
					if depth > limit * scale then
						issue("clip_self", pair[1] .. "->" .. pair[2], a.seen or b.seen, depth)
					end
				end
			end

			-- Another character.
			for otherWho, otherBody in pairs(bodies) do
				if otherWho > who then
					for _, name in ipairs(BODY) do
						for _, otherName in ipairs(BODY) do
							local a, b = parts[name], otherBody.parts[otherName]
							if a and b then
								local depth = overlap(a.cf, a.size * SHRINK, b.cf, b.size * SHRINK)
								if depth > DEPTH * scale then
									issue("clip_other", name .. "->" .. otherWho .. "." .. otherName, a.seen or b.seen, depth)
								end
							end
						end
					end
				end
			end

			-- Held props through the holder's own head, torso or legs, or through scenery.
			for _, hold in ipairs(entry.holds or {}) do
				for _, piece in ipairs(hold.model:GetDescendants()) do
					if piece:IsA("BasePart") and solid(piece) then
						for _, name in ipairs({ "Head", "UpperTorso", "LowerTorso", "LeftUpperLeg", "RightUpperLeg" }) do
							local b = parts[name]
							if b then
								local depth = overlap(piece.CFrame, piece.Size * 0.9, b.cf, b.size * SHRINK)
								if depth > 0.05 * scale then
									issue("clip_prop", tostring(hold.spec.prop) .. "->" .. name, b.seen or seen(piece.Position), depth)
								end
							end
						end
						for _, other in ipairs(workspace:GetPartBoundsInBox(piece.CFrame, piece.Size, scenery)) do
							if solid(other) then
								local depth = overlap(piece.CFrame, piece.Size * 0.9, other.CFrame, other.Size)
								if depth > 0.05 * scale then
									issue("clip_prop", tostring(hold.spec.prop) .. "->" .. other.Name, seen(piece.Position), depth)
								end
							end
						end
					end
				end
			end

			-- Arms: where each upper arm points in the chest's frame, and whether the hand is doing anything.
			local torso = parts.UpperTorso
			local raisedArms, splayed = {}, 0
			metrics.arms = {}
			if torso then
				-- Measured between the joints themselves and against the same body at rest, so a broad-shouldered
				-- body whose arms hang at an angle is not read as holding them out.
				local skeleton, atRest = Pose.skeleton(entry.model), Pose.solve(entry.model, {})
				for side, word in pairs(SIDES) do
					local upper, lower, hand = parts[word .. "UpperArm"], parts[word .. "LowerArm"], parts[word .. "Hand"]
					if upper and lower and hand then
						local sign = side == "R" and 1 or -1
						local shoulderAt, elbowAt = skeleton[word .. "UpperArm"].c1.Position, skeleton[word .. "LowerArm"].c1.Position
						local shoulder = upper.cf * shoulderAt
						local elbow = lower.cf * elbowAt
						local direction = torso.cf:VectorToObjectSpace(elbow - shoulder).Unit
						local resting = atRest.UpperTorso:VectorToObjectSpace(atRest[word .. "LowerArm"] * elbowAt - atRest[word .. "UpperArm"] * shoulderAt).Unit
						local raised = angle(direction, Vector3.new(0, -1, 0))
						local sideways = math.deg(math.asin(math.clamp(direction.X * sign, -1, 1))) - math.deg(math.asin(math.clamp(resting.X * sign, -1, 1)))
						local bend = angle(elbow - shoulder, hand.cf.Position - elbow)
						local near = math.huge
						for _, name in ipairs({ "Head", "UpperTorso", "LowerTorso" }) do
							if parts[name] then
								near = math.min(near, distanceToBox(hand.cf.Position, parts[name].cf, parts[name].size))
							end
						end
						metrics.arms[side] = { raised = round(raised, 0), sideways = round(sideways, 0), bend = round(bend, 0), hand_from_body = round(near), busy = busy[side] }
						if not busy[side] then
							if raised > ARM_RAISED and near > NEAR_BODY * scale then
								table.insert(raisedArms, side)
							end
							if sideways > ARM_SPLAYED and raised <= ARM_RAISED then
								splayed += 1
							end
						end
					end
				end
			end
			local armsSeen = (parts.LeftHand and parts.LeftHand.seen) or (parts.RightHand and parts.RightHand.seen)
				or (parts.LeftLowerArm and parts.LeftLowerArm.seen) or (parts.RightLowerArm and parts.RightLowerArm.seen)
			if #raisedArms == 2 then
				issue("arms_out", "both arms held out with nothing in hand and no target", armsSeen)
			elseif #raisedArms == 1 then
				local word = SIDES[raisedArms[1]]
				issue("arm_out", raisedArms[1] .. " arm held out with nothing in hand and no target", parts[word .. "Hand"].seen or parts[word .. "LowerArm"].seen)
			end
			if splayed == 2 then
				issue("arms_splayed", "both arms held away from the sides", armsSeen)
			end

			-- Legs: a standing figure with both legs angled outwards is the stock stance, not a person.
			local hips = parts.LowerTorso
			if hips and parts.LeftUpperLeg and parts.RightUpperLeg and parts.LeftLowerLeg and parts.RightLowerLeg then
				local wide = 0
				local standing = math.abs(hips.cf.UpVector.Y) > 0.9
				for side, word in pairs(SIDES) do
					local sign = side == "R" and 1 or -1
					local upper = parts[word .. "UpperLeg"]
					local direction = hips.cf:VectorToObjectSpace(-upper.cf.UpVector)
					local sideways = math.deg(math.asin(math.clamp(direction.X * sign, -1, 1)))
					local forward = angle(direction, Vector3.new(0, -1, 0))
					if sideways > STANCE_WIDE and forward < 40 then
						wide += 1
					end
				end
				local moving = (entry.to - entry.from).Magnitude > 0.05
				if standing and wide == 2 and not moving then
					issue("stance_wide", "both legs angled outwards", (parts.LeftFoot and parts.LeftFoot.seen) or (parts.RightFoot and parts.RightFoot.seen) or parts.LeftLowerLeg.seen or parts.RightLowerLeg.seen)
				end
			end

			-- Joints past what a body can do.
			local joints = Pose.joints(entry.model)
			for _, name in ipairs(BODY) do
				local item = parts[name]
				local kind = name:gsub("^Left", ""):gsub("^Right", "")
				local joint = item and joints[name] and joints[name].joint
				if joint then
					local turn = joint.Transform.Rotation
					local hinge, limit = HINGES[kind], LIMITS[kind]
					if hinge then
						local along = turn.UpVector
						local bend = math.deg(math.atan2(along.Z, along.Y))
						local side = math.deg(math.asin(math.clamp(along.X, -1, 1)))
						local rest = CFrame.Angles(math.rad(bend), 0, 0):Inverse() * turn
						local twist = math.deg(math.atan2(-rest.RightVector.Z, rest.RightVector.X))
						if bend < hinge.bend[1] - 0.5 or bend > hinge.bend[2] + 0.5 or math.abs(side) > hinge.side + 0.5 or math.abs(twist) > hinge.twist + 0.5 then
							issue("joint_limit", string.format("%s bend %.0f side %.0f twist %.0f", name, bend, side, twist), item.seen)
						end
					elseif kind == "UpperLeg" then
						local sign = name:sub(1, 4) == "Left" and -1 or 1
						local down = -turn.UpVector
						local forward = math.deg(math.atan2(-down.Z, -down.Y))
						local out = math.deg(math.asin(math.clamp(down.X * sign, -1, 1)))
						if forward < THIGH.forward[1] - 0.5 or forward > THIGH.forward[2] + 0.5 or out < THIGH.out[1] - 0.5 or out > THIGH.out[2] + 0.5 then
							issue("joint_limit", string.format("%s forward %.0f out %.0f", name, forward, out), item.seen)
						end
					elseif limit then
						local rx, ry, rz = turn:ToEulerAnglesYXZ()
						local x, y, z = math.deg(rx), math.deg(ry), math.deg(rz)
						if x < limit.x[1] - 0.5 or x > limit.x[2] + 0.5 or math.abs(y) > limit.y + 0.5 or math.abs(z) > limit.z + 0.5 then
							issue("joint_limit", string.format("%s %.0f/%.0f/%.0f", name, x, y, z), item.seen)
						end
					end
				end
			end

			-- Doing something: a walk, or a named pose that is an action and not the resting stance.
			local action = entry.action
			if action == nil and entry.action_known == nil then
				local pose = entry.spec.pose
				if (entry.to - entry.from).Magnitude > 0.05 then
					action = "walking"
				elseif pose ~= nil and pose ~= "still" then
					action = type(pose) == "string" and pose or "keyed poses"
				end
			end
			metrics.action = action or false
			if not action then
				issue("no_action", "stands in the resting stance with nothing to do", anySeen)
			end

			actors[who] = { issues = issues, metrics = metrics }
		end
		table.insert(out, { u = u, frame = frameIndex, actors = actors })
	end
	if camera then
		stage.cameras[shot.id] = nil
	end
	stage.current = nil
	return { shot = shot.id, samples = out }
end

M.overlap = overlap

return M
