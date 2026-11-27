local Util = __require("util")
local Rig = __require("rig")
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
			spec = actor, model = model, scale = model:GetAttribute("Scale") or 1,
			from = from.Position, to = actor.to and self:place(actor.to).Position or from.Position,
			yaw = yawOf(from), holds = {},
		}
		present[actor.who] = true
		state.actors[actor.who] = entry
		table.insert(state.order, entry)
	end
	for _, model in ipairs(self.castFolder:GetChildren()) do
		if not present[model.Name] then
			Rig.apply(model, PARK, {})
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
	self:pose(0, 0)
	for _, entry in ipairs(state.order) do
		entry.origin = entry.position
	end
	state.camera = Camera.plan(shot.camera, {
		set = set,
		actors = state.actors,
		place = function(spec)
			return self:place(spec)
		end,
	})
	self:applyLighting(shot.time or set.lighting or "day", state.camera.from)
	return state
end

function M:pose(t, u, cameraPos)
	local state = self.current
	for _, entry in ipairs(state.order) do
		local spec = entry.spec
		local e = Util.ease(spec.move_ease or "linear", u)
		entry.position = entry.from:Lerp(entry.to, e)
		entry.rootCF = CFrame.new(entry.position) * Util.yaw(entry.yaw)
		entry.pose = Motion.eval(self.lib, spec, t, u)
		local cfs = Rig.solve(entry.rootCF, entry.pose, entry.scale)
		entry.head = cfs.Head.Position
		entry.facing = Util.yaw(entry.yaw + ((entry.pose.root and entry.pose.root[5]) or 0))
	end
	for _, entry in ipairs(state.order) do
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
		local leader = state.actors[type(plan.follow) == "string" and plan.follow or (type(shot.camera.from) == "table" and shot.camera.from.on) or shot.camera.on]
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
		local value = Util.lerp(item.from, item.to, Util.ease(item.ease, u))
		item.value = value
		Sets.movePiece(state.set, item.piece, value, pos)
	end
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
		dof.Enabled = (shot.camera.dof ~= false) and distance < (focus.within or 14)
		dof.FocusDistance = distance
		dof.InFocusRadius = math.max(2.5, distance * (focus.radius or 0.45))
		dof.FarIntensity = focus.far or 0.4
	end
	return { shot = shot.id, index = index, t = t, u = u }
end

return M
