local Util = __require("util")
local Rig = __require("rig")
local Avatar = __require("avatar")
local Pose = __require("pose")
local Faces = __require("faces")
local Motion = __require("motion")
local Camera = __require("camera")
local Props = __require("props")
local Sets = __require("sets")
local LightingService = game:GetService("Lighting")

local M = {}
M.__index = M

local FACINGS = { camera = 180, front = 180, back = 0, left = 90, right = -90 }
local PARK = CFrame.new(0, -2000, 0)

function M.new(timeline, lib, options)
	local self = setmetatable({}, M)
	self.timeline = timeline
	self.lib = lib
	self.options = options or {}
	self.root = workspace:FindFirstChild("Director")
	if not self.root then
		error("workspace.Director is missing; run the build command first")
	end
	self.sets = Sets.registry(self.root)
	self.castFolder = Util.folder(self.root, "Cast")
	self.propFolder = Util.folder(self.root, "ShotProps")
	self.current = nil
	return self
end

function M:place(spec)
	local set = self.current.set
	if type(spec) == "string" then
		local anchor = set.anchors[spec]
		if not anchor then
			error("set '" .. set.name .. "' has no anchor '" .. spec .. "' (shot " .. self.current.shot.id .. ")")
		end
		return set.origin * CFrame.new(anchor.pos[1], anchor.pos[2], anchor.pos[3]) * Util.yaw(anchor.yaw or 0)
	elseif type(spec) == "table" and spec.anchor then
		local base = self:place(spec.anchor)
		local offset = spec.offset or { 0, 0, 0 }
		local position = base.Position + Vector3.new(offset[1], offset[2], offset[3])
		if spec.yaw then
			return CFrame.new(position) * Util.yaw(spec.yaw)
		end
		return CFrame.new(position) * (base - base.Position)
	elseif type(spec) == "table" then
		return set.origin * CFrame.new(spec[1], spec[2], spec[3])
	end
	error("bad position in shot " .. self.current.shot.id)
end

local function yawOf(cf)
	local look = cf.LookVector
	return math.deg(math.atan2(-look.X, -look.Z))
end

local function yawToward(from, to)
	local delta = to - from
	return math.deg(math.atan2(-delta.X, -delta.Z))
end

local sunTable

local function solveSun(want)
	if not sunTable then
		sunTable = {}
		local clock, latitude = LightingService.ClockTime, LightingService.GeographicLatitude
		for lat = -90, 90, 3 do
			LightingService.GeographicLatitude = lat
			for step = 0, 239 do
				LightingService.ClockTime = step / 10
				local direction = LightingService:GetSunDirection()
				if direction.Y > 0.15 then
					table.insert(sunTable, { direction, step / 10, lat })
				end
			end
		end
		LightingService.ClockTime, LightingService.GeographicLatitude = clock, latitude
	end
	local best, bestDot = nil, -2
	for _, entry in ipairs(sunTable) do
		local dot = entry[1]:Dot(want)
		if dot > bestDot then
			best, bestDot = entry, dot
		end
	end
	return best[2], best[3]
end

local EFFECTS = { grade = "ColorCorrectionEffect", bloom = "BloomEffect", atmosphere = "Atmosphere", sunrays = "SunRaysEffect" }

function M:applyLighting(name, view)
	local preset = self.lib.lighting[name]
	if not preset then
		error("unknown lighting preset '" .. tostring(name) .. "'")
	end
	for key, value in pairs(preset.lighting or {}) do
		if type(value) == "string" then
			value = Color3.fromHex(value)
		end
		LightingService[key] = value
	end
	local key = preset.key
	if key and view then
		local back = Vector3.new(view.pos.X - view.look.X, 0, view.pos.Z - view.look.Z)
		back = back.Magnitude > 0.01 and back.Unit or Vector3.zAxis
		if key.fixed then
			back = Vector3.zAxis
		end
		local turned = CFrame.Angles(0, math.rad(-(key.azimuth or 0)), 0):VectorToWorldSpace(back)
		local elevation = math.rad(key.elevation or 40)
		local want = turned * math.cos(elevation) + Vector3.yAxis * math.sin(elevation)
		local clock, latitude = solveSun(want)
		if math.abs(clock - LightingService.ClockTime) > 0.01 or latitude ~= LightingService.GeographicLatitude then
			self.relit = true
		end
		LightingService.ClockTime, LightingService.GeographicLatitude = clock, latitude
	end
	for effectName, className in pairs(EFFECTS) do
		local effect = LightingService:FindFirstChild("Director_" .. effectName)
		if not effect then
			effect = Instance.new(className)
			effect.Name = "Director_" .. effectName
			effect.Parent = LightingService
		end
		local values = preset[effectName]
		if effectName == "sunrays" then
			effect.Enabled = values ~= nil
		end
		for property, value in pairs(values or {}) do
			if type(value) == "string" then
				value = Color3.fromHex(value)
			end
			effect[property] = value
		end
	end
	self.focus = preset.focus or {}
end

local BLINK = {
	{ t = 0, face = { LeftEyeClosed = 0, RightEyeClosed = 0 } },
	{ t = 3.0, face = { LeftEyeClosed = 0, RightEyeClosed = 0 } },
	{ t = 3.07, face = { LeftEyeClosed = 1, RightEyeClosed = 1 } },
	{ t = 3.2, face = { LeftEyeClosed = 0, RightEyeClosed = 0 } },
	{ t = 3.5, face = { LeftEyeClosed = 0, RightEyeClosed = 0 } },
}

local function faceAt(lib, keys, u)
	local chosen = keys[1][2]
	for _, key in ipairs(keys) do
		if u >= key[1] then
			chosen = key[2]
		end
	end
	local found = lib.expressions[chosen]
	if not found then
		error("unknown expression '" .. tostring(chosen) .. "'")
	end
	return found
end

function M:planAvatar(entry, duration)
	local spec = entry.spec
	local layers = {}
	local anim = spec.anim
	local travel = (entry.to - entry.from).Magnitude
	entry.moving = travel > 0.05
	if anim == nil then
		anim = false
	end
	if anim then
		local options = type(anim) == "table" and anim or { name = anim }
		local found = self.lib.animations[options.name]
		if not found then
			error("unknown animation '" .. tostring(options.name) .. "'")
		end
		local speed = options.speed or found.speed or 1
		if entry.moving and not options.speed and found.stride then
			speed = Util.clamp(travel / math.max(duration, 0.1) / (found.stride * entry.scale), 0.3, 1.8)
		end
		table.insert(layers, {
			content = "rbxassetid://" .. found.id, speed = speed, phase = options.phase or Util.hash(spec.who),
			loop = found.loop ~= false, hold = options.hold, priority = Enum.AnimationPriority.Core,
		})
	end
	entry.based = anim and true or false
	entry.duration = duration
	entry.layers = layers
	entry.rootCF = CFrame.new(entry.from) * Util.yaw(entry.yaw)
	Avatar.stage(entry.model, layers)
	entry.head = Avatar.apply(entry.model, entry.rootCF, 0, 0)
	entry.position = entry.from
	entry.facing = Util.yaw(entry.yaw)
end

-- The pose layer: every joint keyed from the pose library (see pose.luau), plus the face and the blink.
function M:overlayAvatar(entry, state)
	local spec = entry.spec
	local info = Pose.describe(self.lib, spec, entry.moving)
	info.look_strength = spec.look_strength
	info.seed = Util.hash(tostring(spec.who) .. tostring(state.shot.id))
	entry.action = info.action or false
	entry.action_known = true
	entry.wants_prop = info.holds
	local faceKeys = spec.face
	if type(faceKeys) == "string" then
		faceKeys = { { 0, faceKeys } }
	end
	local times = { 0, 1 }
	for _, key in ipairs(info.keys) do
		table.insert(times, key.t)
	end
	if info.dense then
		local steps = math.clamp(math.ceil(entry.duration * 14), 4, 48)
		for step = 1, steps - 1 do
			table.insert(times, step / steps)
		end
	end
	for _, key in ipairs(faceKeys or {}) do
		if key[1] > 0.03 then
			table.insert(times, key[1] - 0.03)
		end
		table.insert(times, key[1])
	end
	table.sort(times)
	local lift = Pose.restLift(entry.model)
	local lookAt = spec.look_at
	local ctx = {
		duration = entry.duration,
		base = entry.based,
		root = function(u)
			local position = entry.from:Lerp(entry.to, Util.ease(spec.move_ease or "linear", u))
			return CFrame.new(position) * Util.yaw(entry.yaw) * CFrame.new(0, lift, 0)
		end,
		place = function(target)
			local ok, found = pcall(function()
				return self:place(target)
			end)
			return ok and found.Position or nil
		end,
		look = lookAt and function(u)
			if lookAt == "camera" then
				return state.camera.from.pos:Lerp(state.camera.to.pos, u)
			elseif state.actors[lookAt] then
				local other = state.actors[lookAt]
				return other.head + (other.to - other.from) * u
			end
			return self:place(lookAt).Position
		end or nil,
	}
	Pose.prime(self.lib, entry.model, info, ctx)
	entry.poseAt = function(u)
		return Pose.jointsAt(self.lib, entry.model, info, ctx, u)
	end
	local layers = table.clone(entry.layers)
	local closesEyes = false
	local keys = {}
	local last = -1
	for _, time in ipairs(times) do
		if time - last > 0.004 then
			last = time
			local key = { t = time }
			local joints = Pose.jointsAt(self.lib, entry.model, info, ctx, time)
			if next(joints) then
				key.joints = joints
			end
			if faceKeys then
				key.face = faceAt(self.lib, faceKeys, time)
				if (key.face.LeftEyeClosed or 0) > 0 then
					closesEyes = true
				end
			end
			table.insert(keys, key)
		end
	end
	table.insert(layers, { content = Avatar.clip(keys, { full = not (info.upper and entry.based), linear = info.dense, model = entry.model }), normalized = true, priority = Enum.AnimationPriority.Action4 })
	if spec.blink ~= false and not closesEyes and not entry.model:GetAttribute("Faceless") then
		table.insert(layers, { content = Avatar.clip(BLINK), speed = 1, phase = Util.hash(spec.who .. "blink"), priority = Enum.AnimationPriority.Action3 })
	end
	entry.layers = layers
	Avatar.stage(entry.model, layers)
end

function M:prepare(index)
	local shot = self.timeline.shots[index]
	local set = self.sets[shot.set]
	if not set then
		error("shot " .. shot.id .. " uses unknown set '" .. tostring(shot.set) .. "'")
	end
	local state = { index = index, shot = shot, set = set, actors = {}, order = {}, props = {}, pieces = {} }
	self.current = state
	self.propFolder:ClearAllChildren()
	local present = {}
	for _, actor in ipairs(shot.cast or {}) do
		local model = self.castFolder:FindFirstChild(actor.who)
		if not model then
			error("cast member '" .. actor.who .. "' has not been built")
		end
		local from = self:place(actor.at)
		local entry = {
			spec = actor, model = model, scale = model:GetAttribute("Scale") or 1, avatar = model:GetAttribute("Rig") == "avatar",
			from = from.Position, to = actor.to and self:place(actor.to).Position or from.Position,
			yaw = yawOf(from), holds = {},
		}
		present[actor.who] = true
		state.actors[actor.who] = entry
		table.insert(state.order, entry)
	end
	for _, model in ipairs(self.castFolder:GetChildren()) do
		if not present[model.Name] then
			if model:GetAttribute("Rig") == "avatar" then
				Avatar.park(model)
			else
				Rig.apply(model, PARK, {})
			end
		end
	end
	for _, entry in ipairs(state.order) do
		local facing = entry.spec.facing
		if type(facing) == "number" then
			entry.yaw = facing
		elseif type(facing) == "string" then
			if FACINGS[facing] then
				entry.yaw = FACINGS[facing]
			elseif state.actors[facing] then
				entry.yaw = yawToward(entry.from, state.actors[facing].from)
			else
				entry.yaw = yawToward(entry.from, self:place(facing).Position)
			end
		elseif entry.spec.to then
			entry.yaw = yawToward(entry.from, entry.to)
		end
		local holds = entry.spec.holds
		if holds then
			if holds.prop then
				holds = { holds }
			end
			for _, hold in ipairs(holds) do
				local model = Props.build(self.propFolder, hold, self.lib)
				local grip = CFrame.new(Util.vec(hold.offset or { 0, 0, 0 })) * CFrame.Angles(math.rad(hold.pitch or 0), math.rad(hold.yaw or 0), math.rad(hold.roll or 0))
				table.insert(entry.holds, { model = model, hand = hold.hand or "R", grip = grip, spec = hold })
			end
		end
	end
	for _, spec in ipairs(shot.props or {}) do
		local model = Props.build(self.propFolder, spec, self.lib)
		local base = self:place(spec.at) * CFrame.Angles(0, math.rad(spec.yaw or 0), 0) * CFrame.Angles(math.rad(spec.pitch or 0), 0, math.rad(spec.roll or 0))
		local target = spec.to and (self:place(spec.to) * CFrame.Angles(0, math.rad(spec.yaw or 0), 0) * CFrame.Angles(math.rad(spec.pitch or 0), 0, math.rad(spec.roll or 0))) or base
		table.insert(state.props, { model = model, base = base, target = target, spec = spec, scaled = 1 })
	end
	for name, piece in pairs(set.pieces) do
		local wanted = shot.set_state and shot.set_state[name]
		if type(wanted) == "number" then
			wanted = { wanted, wanted }
		end
		state.pieces[name] = { piece = piece, from = wanted and (wanted.from or wanted[1]) or 0, to = wanted and (wanted.to or wanted[2]) or 0, ease = wanted and wanted.ease or "inout" }
	end
	local duration = math.max(1, shot.frame_end - shot.frame_start) / self.timeline.fps
	for _, entry in ipairs(state.order) do
		if entry.avatar then
			self:planAvatar(entry, duration)
		end
	end
	self:pose(0, 0)
	for _, entry in ipairs(state.order) do
		entry.origin = entry.position
	end
	local override = self.cameras and self.cameras[shot.id]
	state.cameraSpec = override or shot.camera
	state.camera = Camera.plan(state.cameraSpec, {
		set = set,
		actors = state.actors,
		place = function(spec)
			return self:place(spec)
		end,
	})
	for _, entry in ipairs(state.order) do
		if entry.avatar then
			self:overlayAvatar(entry, state)
			entry.hidden = state.camera.hide == entry.spec.who
		end
	end
	local presetName = shot.time or set.lighting or "day"
	self:applyLighting(presetName, state.camera.from)
	self:prepareLights(state)
	self:prepareSubjectLights(state, self.lib.lighting[presetName])
	return state
end

-- Dark presets carry a "subject" light: a soft fill that rides beside the camera so a face stays readable,
-- or, for a silhouette, a light behind the figure so its outline stands against the background.
function M:prepareSubjectLights(state, preset)
	state.subjectLights = {}
	local spec = state.shot.subject_light
	if spec == nil then
		spec = preset.subject
	end
	if not spec then
		return
	end
	for _, entry in ipairs(state.order) do
		if entry.avatar and entry.spec.subject_light ~= false then
			local back = entry.model:GetAttribute("Silhouette") == true
			local use = back and (spec.back or {}) or spec
			if back and not spec.back then
				continue
			end
			local part = Instance.new("Part")
			part.Name = "SubjectLight"
			part.Anchored, part.CanCollide, part.CanQuery, part.CanTouch = true, false, false, false
			part.Transparency = 1
			part.Size = Vector3.new(0.2, 0.2, 0.2)
			local light = Instance.new("PointLight")
			light.Brightness = (use.brightness or 1) * (entry.spec.subject_level or 1)
			light.Range = use.range or 9
			light.Color = Util.color(use.color or "#a8b8ff")
			light.Shadows = false
			light.Parent = part
			part.Parent = self.propFolder
			table.insert(state.subjectLights, { entry = entry, part = part, back = back, spec = use })
		end
	end
end

function M:moveSubjectLights(state, cameraPos)
	for _, item in ipairs(state.subjectLights or {}) do
		local head = item.entry.head
		local toCamera = cameraPos - head
		local flat = Vector3.new(toCamera.X, 0, toCamera.Z)
		flat = flat.Magnitude > 0.01 and flat.Unit or Vector3.new(0, 0, 1)
		local side = flat:Cross(Vector3.new(0, 1, 0))
		local spec = item.spec
		local reach = math.min(spec.reach or 3, toCamera.Magnitude * 0.7)
		if item.back then
			item.part.Position = head - flat * (spec.reach or 2.5) + Vector3.new(0, spec.up or 0.5, 0)
		else
			item.part.Position = head + flat * reach + side * (spec.side or 1.6) + Vector3.new(0, spec.up or 1.2, 0)
		end
	end
end

function M:pose(t, u, cameraPos)
	local state = self.current
	for _, entry in ipairs(state.order) do
		local spec = entry.spec
		local e = Util.ease(spec.move_ease or "linear", u)
		entry.position = entry.from:Lerp(entry.to, e)
		entry.rootCF = CFrame.new(entry.position) * Util.yaw(entry.yaw)
		if entry.avatar then
			entry.head = Avatar.apply(entry.model, entry.hidden and PARK or entry.rootCF, t, u)
			if entry.hidden then
				entry.head = entry.rootCF.Position + Vector3.new(0, 4.6 * entry.scale, 0)
			end
			entry.facing = Util.yaw(entry.yaw)
			if entry.poseAt and not entry.hidden then
				local _, points = entry.poseAt(u)
				entry.reach = points
			end
			for _, hold in ipairs(entry.holds) do
				local hand = Avatar.hand(entry.model, hold.hand)
				local turn = hold.spec.turn
				if turn == "eyes" then
					local head = entry.model.Head
					hold.model:PivotTo(CFrame.lookAt(hand.Position, head.CFrame * Vector3.new(0, head.Size.Y * 0.1, -head.Size.Z * 0.3)) * hold.grip)
				elseif turn == "ear" then
					-- A phone on a call: flat against the side of the head, running from the ear down to the mouth, screen to the cheek.
					local head = entry.model.Head
					local side = hold.hand == "L" and -1 or 1
					local at = head.CFrame * Vector3.new(side * head.Size.X * 0.56, -head.Size.Y * 0.12, -head.Size.Z * 0.12)
					local along = head.CFrame:VectorToWorldSpace(Vector3.new(0, 0.82, 0.57)).Unit
					local out = head.CFrame:VectorToWorldSpace(Vector3.new(side, 0, 0))
					hold.model:PivotTo(CFrame.fromMatrix(at, along:Cross(out), along, out) * hold.grip)
				elseif turn == "forward" then
					local chest = entry.model.UpperTorso.CFrame
					hold.model:PivotTo(CFrame.lookAt(hand.Position, hand.Position + chest.LookVector) * hold.grip)
				else
					hold.model:PivotTo(hold.spec.follow_arm == false and (CFrame.new(hand.Position) * entry.facing * hold.grip) or (hand * hold.grip))
				end
			end
			continue
		end
		entry.pose = Motion.eval(self.lib, spec, t, u)
		local cfs = Rig.solve(entry.rootCF, entry.pose, entry.scale)
		entry.head = cfs.Head.Position
		entry.facing = Util.yaw(entry.yaw + ((entry.pose.root and entry.pose.root[5]) or 0))
	end
	for _, entry in ipairs(state.order) do
		if entry.avatar then
			continue
		end
		local spec = entry.spec
		local lookAt = spec.look_at
		if lookAt then
			local target
			if lookAt == "camera" then
				target = cameraPos
			elseif state.actors[lookAt] then
				target = state.actors[lookAt].head
			else
				target = self:place(lookAt).Position
			end
			if target then
				Motion.lookAt(entry.pose, CFrame.new(entry.head), CFrame.new(entry.position) * entry.facing, target, spec.look_strength)
			end
		end
		local cfs = Rig.apply(entry.model, entry.rootCF, entry.pose)
		entry.head = cfs.Head.Position
		local faceName = spec.face or "neutral"
		if type(faceName) == "table" then
			local chosen = faceName[1][2]
			for _, key in ipairs(faceName) do
				if u >= key[1] then
					chosen = key[2]
				end
			end
			faceName = chosen
		end
		local face = self.lib.faces[faceName]
		if not face then
			error("unknown face '" .. tostring(faceName) .. "'")
		end
		local blink = spec.blink ~= false and ((t + Util.hash(spec.who) * 3.1) % 3.1) < 0.11
		Faces.set(entry.model, face, blink)
		for _, hold in ipairs(entry.holds) do
			local hand = Rig.hand(cfs, hold.hand, entry.scale)
			local cf
			if hold.spec.follow_arm then
				cf = hand * hold.grip
			else
				cf = CFrame.new(hand.Position) * entry.facing * hold.grip
			end
			hold.model:PivotTo(cf)
		end
	end
end

function M:prepareLights(state)
	state.lights = {}
	for _, holder in ipairs(state.set.model:GetDescendants()) do
		if holder:IsA("BasePart") and holder.Name:sub(1, 6) == "Light_" then
			local light = holder:FindFirstChildWhichIsA("Light")
			if light then
				local name = holder.Name:sub(7)
				local base = light:GetAttribute("Base") or light.Brightness
				light:SetAttribute("Base", base)
				local color = light:GetAttribute("BaseColor") or light.Color
				light:SetAttribute("BaseColor", color)
				light.Brightness = base
				light.Color = color
				local spec = state.shot.lights and state.shot.lights[name]
				if spec == nil then
					local defaults = self.timeline.defaults and self.timeline.defaults.lights and self.timeline.defaults.lights[state.set.name]
					spec = defaults and defaults[name]
				end
				if spec == nil and light:GetAttribute("Off") then
					spec = { off = true }
				end
				table.insert(state.lights, { light = light, base = base, color = color, spec = spec, holder = holder })
			end
		end
	end
end

function M:animateLights(state, t, u)
	for _, item in ipairs(state.lights or {}) do
		local spec = item.spec
		local level = 1
		if spec then
			level = spec.level or 1
			if spec.off then
				level = 0
			end
			if spec.on and (u < spec.on[1] or u > (spec.on[2] or 1)) then
				level = spec.else_level or 0
			end
			if spec.fade then
				level = level * Util.lerp(spec.fade[1], spec.fade[2], u)
			end
			if spec.flicker then
				local wave = math.sin(t * 31 + 1.7) + math.sin(t * 17.3) + math.sin(t * 7.1 + 0.4)
				if wave > 1.1 - spec.flicker then
					level = level * (spec.dip or 0.12)
				end
			end
			if spec.pulse then
				local colors = spec.pulse.colors or { "#ff2020", "#2040ff" }
				local step = math.floor(t * (spec.pulse.hz or 3)) % #colors + 1
				item.light.Color = Color3.fromHex(colors[step])
			end
		end
		item.light.Brightness = item.base * level
		for _, glow in ipairs(item.holder:GetChildren()) do
			if glow:IsA("BasePart") then
				local base = glow:GetAttribute("BaseColor") or glow.Color
				glow:SetAttribute("BaseColor", base)
				glow.Color = base:Lerp(Color3.new(0.02, 0.02, 0.03), 1 - Util.clamp(level, 0, 1))
			end
		end
	end
end

function M:frame(frameIndex)
	local timeline = self.timeline
	local index
	for position, shot in ipairs(timeline.shots) do
		if frameIndex >= shot.frame_start and frameIndex < shot.frame_end then
			index = position
			break
		end
	end
	if not index then
		index = #timeline.shots
	end
	if not self.current or self.current.index ~= index then
		self:prepare(index)
	end
	local state = self.current
	local shot = state.shot
	local length = math.max(1, shot.frame_end - shot.frame_start)
	local t = (frameIndex - shot.frame_start) / timeline.fps
	local u = Util.clamp((frameIndex - shot.frame_start) / length, 0, 1)
	local follow
	local plan = state.camera
	if plan.follow then
		local leader = state.actors[type(plan.follow) == "string" and plan.follow or (type(state.cameraSpec.from) == "table" and state.cameraSpec.from.on) or state.cameraSpec.on]
		if leader then
			local e = Util.ease(leader.spec.move_ease or "linear", u)
			follow = leader.from:Lerp(leader.to, e) - leader.origin
		end
	end
	local pos, look, fov = Camera.at(plan, u, t, follow)
	self:pose(t, u, pos)
	for _, item in ipairs(state.props) do
		local spec = item.spec
		local visible = true
		if spec.show then
			visible = u >= spec.show[1] and u <= (spec.show[2] or 1)
		end
		if not visible then
			item.model:PivotTo(PARK)
		else
			local e = Util.ease(spec.move_ease or "inout", spec.move_window and Util.clamp((u - spec.move_window[1]) / math.max(0.001, spec.move_window[2] - spec.move_window[1]), 0, 1) or u)
			local cf = item.base:Lerp(item.target, e)
			if spec.attach then
				local piece = state.pieces[spec.attach]
				if not piece then
					error("shot " .. shot.id .. ": prop attaches to unknown piece '" .. tostring(spec.attach) .. "'")
				end
				local value = Util.lerp(piece.from, piece.to, Util.ease(piece.ease, u))
				cf = Sets.pieceCFrame(state.set, piece.piece, value) * Sets.pieceCFrame(state.set, piece.piece, 0):ToObjectSpace(cf)
			end
			if spec.spin then
				cf = cf * CFrame.Angles(0, math.rad(spec.spin * t), 0)
			end
			if spec.bob then
				cf = cf + Vector3.new(0, math.sin(t * 2 * math.pi * (spec.bob[2] or 1)) * spec.bob[1], 0)
			end
			if spec.pop and spec.show then
				local since = (u - spec.show[1]) * length / timeline.fps
				local k = Util.ease("back", Util.clamp(since / 0.18, 0.05, 1))
				if math.abs(k - item.scaled) > 0.001 then
					item.model:ScaleTo((spec.scale or 1) * k)
					item.scaled = k
				end
			end
			item.model:PivotTo(cf)
		end
	end
	for _, item in pairs(state.pieces) do
		item.value = Util.lerp(item.from, item.to, Util.ease(item.ease, u))
	end
	for _, item in pairs(state.pieces) do
		local carrier = item.piece.parent and state.pieces[item.piece.parent]
		Sets.movePiece(state.set, item.piece, item.value, pos, carrier and { piece = carrier.piece, value = carrier.value })
	end
	self:animateLights(state, t, u)
	self:moveSubjectLights(state, pos)
	state.view = { pos = pos, look = look, fov = fov, roll = plan.roll }
	local roll = self.options.roll or 0
	local cameraObject = workspace.CurrentCamera
	cameraObject.CameraType = Enum.CameraType.Scriptable
	if roll ~= 0 then
		cameraObject.FieldOfView = fov
	else
		cameraObject.FieldOfView = math.deg(2 * math.atan(math.tan(math.rad(fov / 2)) * Camera.ASPECT))
	end
	cameraObject.CFrame = Camera.cframe(pos, look, roll + plan.roll)
	cameraObject.Focus = CFrame.new(look)
	local dof = LightingService:FindFirstChild("Director_dof")
	if dof then
		local distance = (look - pos).Magnitude
		local focus = self.focus or {}
		-- Close framings get shallow focus so the background falls away; "focus": "shallow" or "deep" on a camera overrides the frame.
		local frame, wish = state.cameraSpec.frame, state.cameraSpec.focus
		local shallow = wish == "shallow" or (wish ~= "deep" and (frame == "close" or frame == "extreme" or frame == "detail"))
		dof.Enabled = (state.cameraSpec.dof ~= false) and (shallow or distance < (focus.within or 14))
		dof.FocusDistance = distance
		dof.InFocusRadius = shallow and math.max(1.2, distance * (focus.shallow_radius or 0.2)) or math.max(2.5, distance * (focus.radius or 0.45))
		dof.FarIntensity = shallow and (focus.shallow_far or 0.9) or focus.far or 0.4
		dof.NearIntensity = shallow and (focus.shallow_near or 0.3) or 0
	end
	return { shot = shot.id, index = index, t = t, u = u }
end

return M
