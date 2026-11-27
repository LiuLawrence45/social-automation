local Util = __require("util")

local M = {}

local JOINTS = { armL = 3, armR = 3, legL = 3, legR = 3, head = 3, torso = 3, root = 6 }

local function get(pose, jointName, index)
	local values = pose[jointName]
	return values and values[index] or 0
end

function M.resolve(lib, pose)
	if type(pose) == "string" then
		local found = lib.poses[pose]
		if not found then
			error("unknown pose '" .. pose .. "'")
		end
		return found
	end
	return pose or {}
end

function M.blend(a, b, u)
	local out = {}
	for jointName, count in pairs(JOINTS) do
		if a[jointName] or b[jointName] then
			local values = {}
			for index = 1, count do
				values[index] = Util.lerp(get(a, jointName, index), get(b, jointName, index), u)
			end
			out[jointName] = values
		end
	end
	return out
end

function M.merge(base, overlay)
	local out = {}
	for jointName, values in pairs(base) do
		out[jointName] = table.clone(values)
	end
	for jointName, values in pairs(overlay or {}) do
		if JOINTS[jointName] then
			out[jointName] = table.clone(values)
		end
	end
	return out
end

local function keyed(lib, keys, u)
	if u <= keys[1].t then
		return M.resolve(lib, keys[1].pose)
	end
	for index = 2, #keys do
		local previous, current = keys[index - 1], keys[index]
		if u <= current.t then
			local span = current.t - previous.t
			local local_u = span > 0 and (u - previous.t) / span or 1
			return M.blend(M.resolve(lib, previous.pose), M.resolve(lib, current.pose), Util.ease(current.ease or "inout", local_u))
		end
	end
	return M.resolve(lib, keys[#keys].pose)
end

function M.eval(lib, actor, t, u)
	local spec = actor.action
	local name = type(spec) == "table" and spec.name or spec
	local options = type(spec) == "table" and spec or {}
	local action
	if name then
		action = lib.actions[name]
		if not action then
			error("unknown action '" .. tostring(name) .. "'")
		end
	else
		action = { base = actor.pose or "stand", osc = actor.still and {} or lib.actions.idle.osc }
	end
	local speed = options.speed or 1
	local phase = (options.phase or 0) + Util.hash(actor.who or "") * 0.0
	local time = math.max(0, t - (options.delay or 0)) * speed
	local pose
	if action.keys then
		local progress = u
		if action.seconds then
			progress = time / action.seconds
			if action.loop then
				progress = progress % 1
			end
		elseif options.delay then
			progress = Util.clamp((u - options.delay) / math.max(0.001, 1 - options.delay), 0, 1)
		end
		pose = M.merge(keyed(lib, action.keys, progress), nil)
	else
		pose = M.merge(M.resolve(lib, action.base or "stand"), nil)
	end
	if actor.pose and name then
		pose = M.merge(pose, M.resolve(lib, actor.pose))
	end
	for _, osc in ipairs(action.osc or {}) do
		local jointName, index, amp, hz, offset, shape = osc[1], osc[2], osc[3], osc[4], osc[5] or 0, osc[6]
		local values = pose[jointName]
		if not values then
			values = {}
			pose[jointName] = values
		end
		local wave = math.sin((time * hz + offset + phase) * 2 * math.pi)
		if shape == "abs" then
			wave = math.abs(wave)
		end
		values[index] = (values[index] or 0) + amp * wave
	end
	if actor.overlay then
		pose = M.merge(pose, actor.overlay)
	end
	return pose
end

function M.lookAt(pose, headCF, bodyYawCF, target, strength)
	local direction = bodyYawCF:PointToObjectSpace(target) - bodyYawCF:PointToObjectSpace(headCF.Position)
	if direction.Magnitude < 0.01 then
		return pose
	end
	local yaw = math.deg(math.atan2(-direction.X, -direction.Z))
	local pitch = math.deg(math.atan2(direction.Y, math.sqrt(direction.X * direction.X + direction.Z * direction.Z)))
	local head = pose.head and table.clone(pose.head) or { 0, 0, 0 }
	head[1] = (head[1] or 0) + Util.clamp(pitch, -45, 45) * (strength or 1)
	head[2] = (head[2] or 0) + Util.clamp(yaw, -70, 70) * (strength or 1)
	pose.head = head
	return pose
end

return M
