local Util = __require("util")

-- Avatar posing: full-body poses from the library, feet or hips planted by measurement, hands placed on
-- targets by two-bone inverse kinematics, small repeating movements, and the head turned to a look target.
local M = {}

local PARENT = {
	LowerTorso = "HumanoidRootPart", UpperTorso = "LowerTorso", Head = "UpperTorso",
	LeftUpperArm = "UpperTorso", LeftLowerArm = "LeftUpperArm", LeftHand = "LeftLowerArm",
	RightUpperArm = "UpperTorso", RightLowerArm = "RightUpperArm", RightHand = "RightLowerArm",
	LeftUpperLeg = "LowerTorso", LeftLowerLeg = "LeftUpperLeg", LeftFoot = "LeftLowerLeg",
	RightUpperLeg = "LowerTorso", RightLowerLeg = "RightUpperLeg", RightFoot = "RightLowerLeg",
}
local ORDER = {
	"LowerTorso", "UpperTorso", "Head",
	"LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand",
	"LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "RightUpperLeg", "RightLowerLeg", "RightFoot",
}
local UPPER = { UpperTorso = true, Head = true, LeftUpperArm = true, LeftLowerArm = true, LeftHand = true, RightUpperArm = true, RightLowerArm = true, RightHand = true }
local WORD = { L = "Left", R = "Right" }
local GROUND = {
	feet = { "LeftFoot", "RightFoot" },
	hips = { "LowerTorso", "LeftUpperLeg", "RightUpperLeg" },
	body = ORDER,
}
-- Places on the body a hand can go: part, position as a share of the part's size, and the way the surface faces.
-- An "s" entry is mirrored for the left hand. The hand stops its own half thickness short of the surface.
local POINTS = {
	mouth = { "Head", { 0, -0.24, -0.5 }, { 0, 0, -1 } },
	chin = { "Head", { 0, -0.46, -0.36 }, { 0, -0.4, -1 } },
	eye = { "Head", { 0.3, 0.04, -0.5 }, { 0, 0, -1 }, s = true },
	brow = { "Head", { 0.1, 0.3, -0.5 }, { 0, 0, -1 }, s = true },
	ear = { "Head", { 0.5, -0.05, 0.02 }, { 1, 0, 0 }, s = true },
	neck = { "Head", { 0.25, -0.5, 0.42 }, { 0.3, 0, 1 }, s = true },
	chest = { "UpperTorso", { 0.24, 0.16, -0.5 }, { 0, 0, -1 }, s = true },
	heart = { "UpperTorso", { -0.16, 0.16, -0.5 }, { 0, 0, -1 } },
	ribs = { "UpperTorso", { -0.4, -0.12, -0.5 }, { 0, 0, -1 }, s = true },
	belly = { "UpperTorso", { -0.12, -0.44, -0.5 }, { 0, 0, -1 }, s = true },
	collar = { "UpperTorso", { -0.36, 0.24, -0.5 }, { 0, 0, -1 }, s = true },
	waist = { "UpperTorso", { -0.3, -0.42, -0.5 }, { 0, 0, -1 }, s = true },
	front = { "UpperTorso", { 0.14, 0.1, -0.5 }, { 0, 0, -1 }, s = true },
	belt = { "LowerTorso", { 0.44, 0.2, -0.5 }, { 0.3, 0, -1 }, s = true },
	hip = { "LowerTorso", { 0.5, 0.1, 0 }, { 1, 0, 0 }, s = true },
	knee = { "LowerLeg", { 0, 0.5, -0.5 }, { 0, 0.6, -1 }, own = true },
	thigh = { "UpperLeg", { 0, 0.0, -0.5 }, { 0, 0, -1 }, own = true },
}
M.ORDER = ORDER
M.PARENT = PARENT

local skeletons = setmetatable({}, { __mode = "k" })

function M.jointCFrame(values)
	return CFrame.new(values[4] or 0, values[5] or 0, values[6] or 0)
		* CFrame.Angles(0, math.rad(values[2] or 0), 0)
		* CFrame.Angles(math.rad(values[1] or 0), 0, 0)
		* CFrame.Angles(0, 0, math.rad(values[3] or 0))
end

local function euler(cf)
	local rx, ry, rz = cf:ToEulerAnglesYXZ()
	return { math.deg(rx), math.deg(ry), math.deg(rz), cf.X, cf.Y, cf.Z }
end

-- The joint that drives each part: a Motor6D on older rigs, an AnimationConstraint between two attachments on newer ones.
function M.joints(model)
	local found = {}
	for _, item in ipairs(model:GetDescendants()) do
		if item:IsA("Motor6D") and item.Part1 then
			found[item.Part1.Name] = { joint = item, c0 = item.C0, c1 = item.C1 }
		elseif item:IsA("AnimationConstraint") and item.Attachment0 and item.Attachment1 then
			local first, second = item.Attachment0, item.Attachment1
			if PARENT[first.Parent.Name] == second.Parent.Name then
				first, second = second, first
			end
			found[second.Parent.Name] = { joint = item, c0 = first.CFrame, c1 = second.CFrame }
		end
	end
	return found
end

function M.skeleton(model)
	local found = skeletons[model]
	if not found then
		found = {}
		local joints = M.joints(model)
		for _, name in ipairs(ORDER) do
			local part = model:FindFirstChild(name)
			local joint = joints[name]
			if not part or not joint then
				error("avatar '" .. model.Name .. "' has no joint for " .. name)
			end
			found[name] = { c0 = joint.c0, c1 = joint.c1, size = part.Size }
		end
		skeletons[model] = found
	end
	return found
end

-- Where every part ends up, in the root part's space, for a set of joint values.
function M.solve(model, joints)
	local skeleton = M.skeleton(model)
	local out = { HumanoidRootPart = CFrame.new() }
	for _, name in ipairs(ORDER) do
		local joint = skeleton[name]
		out[name] = out[PARENT[name]] * joint.c0 * M.jointCFrame(joints[name] or {}) * joint.c1:Inverse()
	end
	return out
end

local function lowest(model, cfs, names)
	local skeleton = M.skeleton(model)
	local low = math.huge
	for _, name in ipairs(names) do
		local cf, half = cfs[name], skeleton[name].size / 2
		for _, sx in ipairs({ -1, 1 }) do
			for _, sy in ipairs({ -1, 1 }) do
				for _, sz in ipairs({ -1, 1 }) do
					low = math.min(low, (cf * Vector3.new(half.X * sx, half.Y * sy, half.Z * sz)).Y)
				end
			end
		end
	end
	return low
end

-- Height of the root part above the ground when the avatar stands at rest, measured on the built body.
function M.restLift(model)
	local lift = model:GetAttribute("RestLift")
	if not lift then
		lift = -lowest(model, M.solve(model, {}), GROUND.feet)
		model:SetAttribute("RestLift", lift)
	end
	return lift
end

local function groundShift(model, joints, mode)
	local names = GROUND[mode or "feet"]
	if not names then
		return 0
	end
	return -M.restLift(model) - lowest(model, M.solve(model, joints), names)
end

local function resolvePose(lib, name)
	local merged = { joints = {}, reach = {}, wobble = {}, actions = {}, rest = true, upper = true, ground = nil, holds = {} }
	for piece in string.gmatch(name, "[^+]+") do
		local found = lib.avatar_poses[piece]
		if not found then
			error("unknown avatar pose '" .. piece .. "'")
		end
		for joint, values in pairs(found.joints or {}) do
			if not PARENT[joint] then
				error("pose '" .. piece .. "' names an unknown joint '" .. tostring(joint) .. "'")
			end
			merged.joints[joint] = values
		end
		for side, reach in pairs(found.reach or {}) do
			merged.reach[side] = reach
		end
		for _, wobble in ipairs(found.wobble or {}) do
			table.insert(merged.wobble, wobble)
		end
		if found.action and not found.rest then
			table.insert(merged.actions, found.action)
		end
		if not found.rest then
			merged.rest = false
		end
		if not found.upper then
			merged.upper = false
		end
		if found.ground then
			merged.ground = found.ground
		end
		if found.holds then
			merged.holds[found.holds] = true
		end
	end
	merged.action = #merged.actions > 0 and table.concat(merged.actions, " and ") or nil
	return merged
end

local function poseKeys(lib, spec, moving)
	local keys = spec.pose
	if keys == nil then
		keys = moving and (lib.avatar_poses._defaults.walk) or lib.avatar_poses._defaults.stand
	end
	if type(keys) == "string" then
		keys = { { t = 0, pose = keys } }
	end
	local out = {}
	for _, key in ipairs(keys) do
		table.insert(out, { t = key.t or 0, ease = key.ease, def = resolvePose(lib, key.pose), name = key.pose })
	end
	table.sort(out, function(a, b)
		return a.t < b.t
	end)
	return out
end

-- What the shot list asks of one avatar: the keyed poses, what it is doing, and what each hand reaches for.
function M.describe(lib, spec, moving)
	local keys = poseKeys(lib, spec, moving)
	local actions, rest, upper, dense, holds = {}, true, true, moving, {}
	for _, key in ipairs(keys) do
		if key.def.action and not table.find(actions, key.def.action) then
			table.insert(actions, key.def.action)
		end
		rest = rest and key.def.rest
		upper = upper and key.def.upper
		dense = dense or #key.def.wobble > 0 or next(key.def.reach) ~= nil
		for side in pairs(key.def.holds) do
			holds[side] = true
		end
	end
	local reach = {}
	for side, value in pairs(spec.reach or {}) do
		reach[side] = type(value) == "table" and value.to ~= nil and value or { to = value }
		dense = true
	end
	if spec.look_at then
		dense = dense or moving
	end
	local action = #actions > 0 and table.concat(actions, ", then ") or nil
	if moving and not action then
		action = "walking"
	end
	return { keys = keys, action = action, rest = rest and not moving, upper = upper, dense = dense, reach = reach, holds = holds }
end

local function blend(a, b, k)
	local out = {}
	for _, source in ipairs({ a, b }) do
		for name in pairs(source) do
			if not out[name] then
				local from, to = a[name] or {}, b[name] or {}
				local values = {}
				for index = 1, 6 do
					values[index] = Util.lerp(from[index] or 0, to[index] or 0, k)
				end
				out[name] = values
			end
		end
	end
	return out
end

local function bodyPoint(model, cfs, name, side, offset, scale)
	local point = POINTS[name]
	if not point then
		return nil
	end
	local sign = side == "R" and 1 or -1
	local partName = point.own and (WORD[side] .. point[1]) or point[1]
	local mirror = point.s and sign or 1
	local size = M.skeleton(model)[partName].size
	local at = Vector3.new(point[2][1] * mirror * size.X, point[2][2] * size.Y, point[2][3] * size.Z)
	local normal = Vector3.new(point[3][1] * mirror, point[3][2], point[3][3]).Unit
	local hand = M.skeleton(model)[WORD[side] .. "Hand"].size
	local cf = cfs[partName]
	local out = cf * (at + normal * (hand.X * 0.5 + 0.04 * scale))
	if offset then
		out += cf:VectorToWorldSpace(Vector3.new(offset[1] * mirror, offset[2], offset[3]) * scale)
	end
	return out, cf:VectorToWorldSpace(normal)
end

local function axesOf(cf)
	return { cf.RightVector, cf.UpVector, cf.LookVector }
end

-- Depth by which two oriented boxes overlap (separating axis test); 0 when they do not.
function M.overlap(cfA, sizeA, cfB, sizeB)
	local a, b = axesOf(cfA), axesOf(cfB)
	local ha = { sizeA.X / 2, sizeA.Y / 2, sizeA.Z / 2 }
	local hb = { sizeB.X / 2, sizeB.Y / 2, sizeB.Z / 2 }
	local delta = cfB.Position - cfA.Position
	local least = math.huge
	local function apart(axis)
		if axis.Magnitude < 1e-4 then
			return false
		end
		axis = axis.Unit
		local reach = 0
		for i = 1, 3 do
			reach += ha[i] * math.abs(a[i]:Dot(axis)) + hb[i] * math.abs(b[i]:Dot(axis))
		end
		local gap = reach - math.abs(delta:Dot(axis))
		if gap <= 0 then
			return true
		end
		least = math.min(least, gap)
		return false
	end
	for i = 1, 3 do
		if apart(a[i]) or apart(b[i]) then
			return 0
		end
	end
	for i = 1, 3 do
		for j = 1, 3 do
			if apart(a[i]:Cross(b[j])) then
				return 0
			end
		end
	end
	return least
end

-- Turns one arm so the hand sits on a point given in root space; returns how far short it fell.
-- The elbow is a hinge about the upper arm's own sideways axis, the forearm turns about its length to face
-- the palm, and the pole says which way the elbow points (in the chest's frame, mirrored for the left arm).
local function reachArm(model, joints, side, target, facing, pole)
	local skeleton = M.skeleton(model)
	local word = WORD[side]
	local upper, lower, hand = word .. "UpperArm", word .. "LowerArm", word .. "Hand"
	local sign = side == "R" and 1 or -1
	local a = (skeleton[upper].c1:Inverse() * skeleton[lower].c0).Position
	local wrist = (skeleton[lower].c1:Inverse() * skeleton[hand].c0).Position
	local palm = skeleton[hand].c1:Inverse().Position
	local existing = joints[hand] or {}
	local goal, short = target, 0
	local foreTwist, handTwist = 0, existing[2] or 0
	for _ = 1, 4 do
		local cfs = M.solve(model, joints)
		local shoulder = cfs.UpperTorso * skeleton[upper].c0
		local fore = CFrame.Angles(0, math.rad(foreTwist), 0) * (wrist + M.jointCFrame({ existing[1] or 0, handTwist, existing[3] or 0 }) * palm)
		local function span(degrees)
			return (a + CFrame.Angles(math.rad(degrees), 0, 0) * fore).Magnitude
		end
		local d = goal - shoulder.Position
		local bend, longest = 0, -1
		for degrees = 0, 40, 2 do
			local value = span(degrees)
			if value > longest then
				bend, longest = degrees, value
			end
		end
		if d.Magnitude < longest then
			local before = longest
			local from = bend
			bend = 150
			for degrees = from + 1, 150 do
				local value = span(degrees)
				if value <= d.Magnitude then
					bend = degrees - (d.Magnitude - value) / math.max(before - value, 1e-6)
					break
				end
				before = value
			end
		end
		local theta = math.rad(bend)
		local reachLocal = a + CFrame.Angles(theta, 0, 0) * fore
		local e1 = reachLocal.Unit
		local hinge = Vector3.new(1, 0, 0)
		hinge = (hinge - e1 * hinge:Dot(e1)).Unit
		local e2 = e1:Cross(hinge)
		local dir = d.Unit
		local toward = cfs.UpperTorso:VectorToWorldSpace(Vector3.new(pole[1] * sign, pole[2], pole[3]))
		local d2 = toward - dir * toward:Dot(dir)
		if d2.Magnitude < 1e-3 then
			d2 = cfs.UpperTorso:VectorToWorldSpace(Vector3.new(sign, 0, 0))
			d2 -= dir * d2:Dot(dir)
		end
		d2 = d2.Unit
		local rotation = CFrame.fromMatrix(Vector3.zero, dir, d2, dir:Cross(d2)) * CFrame.fromMatrix(Vector3.zero, e1, e2, e1:Cross(e2)):Inverse()
		if facing then
			local n = (rotation * CFrame.Angles(theta, 0, 0)):VectorToObjectSpace(facing)
			if math.abs(n.X) + math.abs(n.Z) > 1e-3 then
				local total = math.clamp(math.deg(math.atan2(sign * n.Z, -sign * n.X)), -100, 100)
				foreTwist = math.clamp(total * 0.75, -70, 70)
				handTwist = math.clamp(total - foreTwist, -30, 30)
			end
		end
		joints[upper] = euler(shoulder.Rotation:Inverse() * rotation)
		joints[lower] = euler(CFrame.Angles(theta, 0, 0) * CFrame.Angles(0, math.rad(foreTwist), 0))
		joints[hand] = { existing[1] or 0, handTwist, existing[3] or 0 }
		local reached = M.solve(model, joints)[hand].Position
		short = (reached - target).Magnitude
		goal += target - reached
	end
	return short
end

-- Ways the elbow may point (in the chest's frame, mirrored for the left arm), most natural first, and how far a
-- hand may stand off the body. The choice that keeps the arm out of the body with the elbow low wins.
local POLES = {
	{ 0.55, -1, 0.5 }, { 0.8, -1, 0.2 }, { 0.7, -1, -0.2 }, { 0.9, -1, -0.4 }, { 0.5, -1, -0.5 }, { 0.2, -1, -0.3 },
	{ 0.3, -1, -0.8 }, { 1, -0.6, -0.6 }, { 1, -0.5, 0.3 }, { 1, 0.1, 0.2 }, { 1, 0.3, -0.5 }, { 0.4, -0.6, 1 },
}
local STAND_OFF = { 0, 0.12, 0.25, 0.4 }
local ARM_KEEPS_OUT_OF = { "Head", "UpperTorso", "LowerTorso", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg" }

local function armCost(model, joints, side, target)
	local skeleton = M.skeleton(model)
	local cfs = M.solve(model, joints)
	local word, other = WORD[side], WORD[side == "R" and "L" or "R"]
	local clip = 0
	local function against(limb, name)
		clip += M.overlap(cfs[limb], skeleton[limb].size * 0.82, cfs[name], skeleton[name].size * 0.82)
	end
	for _, limb in ipairs({ word .. "LowerArm", word .. "Hand" }) do
		for _, name in ipairs(ARM_KEEPS_OUT_OF) do
			against(limb, name)
		end
		for _, name in ipairs({ other .. "UpperArm", other .. "LowerArm", other .. "Hand" }) do
			against(limb, name)
		end
	end
	against(word .. "UpperArm", "Head")
	local shoulder = (cfs.UpperTorso * skeleton[word .. "UpperArm"].c0).Position
	local elbow = (cfs[word .. "UpperArm"] * skeleton[word .. "LowerArm"].c0).Position
	local length = (elbow - shoulder).Magnitude
	local elbowUp = cfs.UpperTorso:VectorToObjectSpace(elbow - shoulder).Y
	local targetUp = cfs.UpperTorso:VectorToObjectSpace(target - shoulder).Y
	local allowed = math.max(-0.55 * length, math.min(targetUp, 0.3) - 0.5)
	return 6 * clip + 1.5 * math.max(0, elbowUp - allowed)
end

local function chooseArm(model, joints, side, target, facing, normal, scale)
	local best, bestCost = { pole = POLES[1], off = 0 }, math.huge
	for step, off in ipairs(normal and STAND_OFF or { 0 }) do
		local point = normal and target + normal * (off * scale) or target
		for index, pole in ipairs(POLES) do
			local trial = table.clone(joints)
			local short = reachArm(model, trial, side, point, facing, pole)
			local cost = armCost(model, trial, side, point) + short * 2 + 0.012 * (index - 1) + 0.5 * off + 0.001 * step
			if cost < bestCost then
				best, bestCost = { pole = pole, off = off }, cost
			end
		end
	end
	return best
end

local function amountAt(reach, u)
	local amount = 1
	if reach.from or reach.by then
		local from, by = reach.from or 0, reach.by or 1
		amount = by > from and Util.clamp((u - from) / (by - from), 0, 1) or (u >= by and 1 or 0)
	end
	if reach.release then
		local last = reach.gone or 1
		local out = last > reach.release and Util.clamp((u - reach.release) / (last - reach.release), 0, 1) or (u >= reach.release and 1 or 0)
		amount = math.min(amount, 1 - out)
	end
	return Util.ease("inout", amount)
end

-- Joint values for one avatar at one moment of a shot (u from 0 to 1), and where each reaching hand should be.
-- ctx: duration in seconds, root(u) the root part's CFrame, place(target) a world position for an anchor,
-- look(u) a world point for the head or nil, base true when a stock animation plays underneath.
function M.jointsAt(lib, model, info, ctx, u)
	local keys = info.keys
	local scale = model:GetAttribute("Scale") or 1
	local previous, current, k = keys[1], keys[1], 1
	for index = 2, #keys do
		if u >= keys[index - 1].t then
			previous, current = keys[index - 1], keys[index]
			local span = current.t - previous.t
			k = span > 0 and Util.clamp((u - previous.t) / span, 0, 1) or 1
			k = Util.ease(current.ease or "inout", k)
		end
	end
	if u >= keys[#keys].t then
		previous, current, k = keys[#keys], keys[#keys], 1
	end
	local joints = blend(previous.def.joints, current.def.joints, k)
	local seconds = u * ctx.duration
	for _, def in ipairs(previous == current and { current.def } or { previous.def, current.def }) do
		local weight = previous == current and 1 or (def == current.def and k or 1 - k)
		for _, wobble in ipairs(def.wobble) do
			local values = joints[wobble[1]] or { 0, 0, 0, 0, 0, 0 }
			joints[wobble[1]] = values
			values[wobble[2]] = (values[wobble[2]] or 0) + weight * wobble[3] * math.sin(2 * math.pi * (seconds * wobble[4] + (wobble[5] or 0)))
		end
	end
	local target = ctx.look and ctx.look(u)
	local root = ctx.root(u)
	if target then
		local solved = M.solve(model, joints)
		local chest = root * solved.UpperTorso
		local head = (root * solved.Head).Position
		local direction = chest:VectorToObjectSpace(target - head)
		if direction.Magnitude > 0.01 then
			local strength = info.look_strength or 1
			local yaw = Util.clamp(math.deg(math.atan2(-direction.X, -direction.Z)), -70, 70) * strength
			local pitch = Util.clamp(math.deg(math.atan2(direction.Y, math.sqrt(direction.X * direction.X + direction.Z * direction.Z))), -40, 40) * strength
			local neck = joints.Head or {}
			joints.Head = { Util.clamp((neck[1] or 0) + pitch, -45, 45), Util.clamp((neck[2] or 0) + yaw, -80, 80), neck[3] or 0, neck[4] or 0, neck[5] or 0, neck[6] or 0 }
		end
	end
	if not (previous.def.upper and current.def.upper) then
		local shift = Util.lerp(groundShift(model, joints, previous.def.ground), groundShift(model, joints, current.def.ground), k)
		local hips = joints.LowerTorso or { 0, 0, 0, 0, 0, 0 }
		joints.LowerTorso = { hips[1] or 0, hips[2] or 0, hips[3] or 0, hips[4] or 0, (hips[5] or 0) + shift, hips[6] or 0 }
	end
	local points = {}
	info.poles = info.poles or { L = {}, R = {} }
	for _, side in ipairs({ "R", "L" }) do
		local wanted = {}
		if info.reach[side] then
			table.insert(wanted, { reach = info.reach[side], amount = amountAt(info.reach[side], u) })
		else
			local a, b = previous.def.reach[side], current.def.reach[side]
			if previous == current then
				if a then
					table.insert(wanted, { reach = a, amount = 1 })
				end
			else
				if a then
					table.insert(wanted, { reach = a, amount = 1 - k })
				end
				if b then
					table.insert(wanted, { reach = b, amount = k })
				end
			end
		end
		local free = { joints[WORD[side] .. "UpperArm"] or {}, joints[WORD[side] .. "LowerArm"] or {}, joints[WORD[side] .. "Hand"] or {} }
		local total, mixed = 0, nil
		for _, item in ipairs(wanted) do
			if item.amount > 0.001 then
				local reach = type(item.reach) == "table" and item.reach or { to = item.reach }
				local solved = M.solve(model, joints)
				local point, facing, normal
				if type(reach.to) == "string" and POINTS[reach.to] then
					point, normal = bodyPoint(model, solved, reach.to, reach.side or side, reach.offset, scale)
					facing = reach.palm and solved.UpperTorso:VectorToWorldSpace(Vector3.new(reach.palm[1], reach.palm[2], reach.palm[3]).Unit) or -normal
				else
					local world = ctx.place(reach.to)
					if not world then
						error("avatar '" .. model.Name .. "' reaches for unknown target '" .. tostring(reach.to) .. "'")
					end
					if reach.offset then
						world += Vector3.new(reach.offset[1], reach.offset[2], reach.offset[3])
					end
					point = root:PointToObjectSpace(world)
					if reach.palm then
						facing = root:VectorToObjectSpace(Vector3.new(reach.palm[1], reach.palm[2], reach.palm[3]).Unit)
					end
				end
				local chosen = info.poles[side][item.reach]
				if not chosen then
					chosen = reach.elbow and { pole = reach.elbow, off = 0 } or chooseArm(model, joints, side, point, facing, normal, scale)
					info.poles[side][item.reach] = chosen
				end
				if normal then
					point += normal * (chosen.off * scale)
				end
				local trial = table.clone(joints)
				local short = reachArm(model, trial, side, point, facing, chosen.pole)
				local got = { trial[WORD[side] .. "UpperArm"], trial[WORD[side] .. "LowerArm"], trial[WORD[side] .. "Hand"] }
				if not mixed then
					mixed, total = got, item.amount
				else
					total += item.amount
					for index = 1, 3 do
						mixed[index] = euler(M.jointCFrame(mixed[index]):Lerp(M.jointCFrame(got[index]), item.amount / total))
					end
				end
				points[side] = { point = root:PointToWorldSpace(point), name = type(reach.to) == "string" and reach.to or "place", touch = reach.touch,
					tolerance = reach.tolerance, short = short, active = item.amount > 0.98 }
			end
		end
		if mixed then
			local amount = math.min(total, 1)
			for index, name in ipairs({ WORD[side] .. "UpperArm", WORD[side] .. "LowerArm", WORD[side] .. "Hand" }) do
				joints[name] = amount >= 0.999 and mixed[index] or euler(M.jointCFrame(free[index]):Lerp(M.jointCFrame(mixed[index]), amount))
			end
		end
	end
	if previous.def.upper and current.def.upper and ctx.base then
		for name in pairs(joints) do
			if not UPPER[name] then
				joints[name] = nil
			end
		end
	end
	return joints, points
end

-- Settles which way each elbow points at fixed moments, so that every later frame of the shot agrees.
function M.prime(lib, model, info, ctx)
	local moments = { 1, 0.5 }
	for _, key in ipairs(info.keys) do
		table.insert(moments, key.t)
	end
	for _, reach in pairs(info.reach) do
		table.insert(moments, reach.by or 1)
	end
	for _, u in ipairs(moments) do
		M.jointsAt(lib, model, info, ctx, u)
	end
end

return M
