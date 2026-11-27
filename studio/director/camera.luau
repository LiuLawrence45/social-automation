local Util = __require("util")

local M = {}

M.FOV = 36
M.ASPECT = 16 / 9

local FRAMES = {
	extreme = { h = 2.3, head = 0.5 },
	detail = { h = 3.4, head = 0.42 },
	close = { h = 5.6, head = 0.36 },
	medium = { h = 9, head = 0.33 },
	full = { h = 14, head = 0.27 },
	wide = { h = 22, head = 0.32 },
	vast = { h = 40, head = 0.4 },
}

local ANGLES = { eye = 0, low = -16, worm = -32, high = 24, top = 55 }

local function tanHalfVertical(fov)
	return math.tan(math.rad(fov / 2)) * M.ASPECT
end

local function subject(ctx, name)
	local actor = ctx.actors[name]
	if actor then
		return actor.head, actor.facing
	end
	local place = ctx.place(name)
	return place.Position, place - place.Position
end

function M.resolve(spec, ctx)
	if type(spec) == "string" then
		local found = ctx.set.cameras[spec]
		if not found then
			error("set '" .. ctx.set.name .. "' has no camera '" .. spec .. "'")
		end
		spec = found
	end
	local fov = spec.fov or M.FOV
	if spec.pos then
		return {
			pos = ctx.set.origin:PointToWorldSpace(Util.vec(spec.pos)),
			look = ctx.set.origin:PointToWorldSpace(Util.vec(spec.look)),
			fov = fov,
		}
	end
	if spec.pov then
		local eye, facing = subject(ctx, spec.pov)
		local target = spec.on and subject(ctx, spec.on) or (eye + facing.LookVector * 12)
		local forward = (target - eye).Unit
		local distance = (target - eye).Magnitude
		local wide = spec.fov or 46
		local drop = (0.5 - (spec.head or 0.38)) * 2 * distance * tanHalfVertical(wide)
		return { pos = eye + forward * 0.3, look = target - Vector3.new(0, drop, 0), fov = wide, hide = spec.pov }
	end
	if spec.ots then
		local near = subject(ctx, spec.ots)
		local far = subject(ctx, spec.on)
		local flat = Vector3.new(far.X - near.X, 0, far.Z - near.Z)
		local forward = flat.Magnitude > 0.01 and flat.Unit or Vector3.zAxis
		local right = forward:Cross(Vector3.yAxis)
		local side = spec.side == "left" and -1 or 1
		local pos = near - forward * (spec.back or 3.6) + right * side * (spec.across or 1.0) + Vector3.new(0, spec.up or 0.3, 0)
		local distance = (far - pos).Magnitude
		local drop = (0.5 - (spec.head or 0.36)) * 2 * distance * tanHalfVertical(fov)
		return { pos = pos, look = far - Vector3.new(0, drop, 0), fov = fov }
	end
	local point, facing = subject(ctx, spec.on)
	local frame = FRAMES[spec.frame or "medium"] or FRAMES.medium
	local height = spec.height or frame.h
	local distance = height / (2 * tanHalfVertical(fov))
	local target = point - Vector3.new(0, (0.5 - (spec.head or frame.head)) * height, 0)
	if spec.offset then
		target = target + (facing * CFrame.new(Util.vec(spec.offset))).Position
	end
	local pitch = spec.pitch or ANGLES[spec.angle or "eye"] or 0
	local direction = (facing * CFrame.Angles(0, math.rad(spec.yaw or 0), 0) * CFrame.Angles(math.rad(pitch), 0, 0)).LookVector
	return { pos = target + direction * distance, look = target, fov = fov, height = height }
end

local function moved(from, move, amount)
	local view = from.look - from.pos
	local distance = view.Magnitude
	local forward = view.Unit
	local right = forward:Cross(Vector3.yAxis)
	right = right.Magnitude > 0.001 and right.Unit or Vector3.xAxis
	local k = amount or 1
	local to = { pos = from.pos, look = from.look, fov = from.fov }
	if move == "push" then
		to.pos = from.look - forward * distance * (1 - 0.12 * k)
	elseif move == "pull" then
		to.pos = from.look - forward * distance * (1 + 0.14 * k)
	elseif move == "pan_right" or move == "pan_left" then
		local sign = move == "pan_right" and 1 or -1
		to.look = from.look + right * distance * 0.16 * k * sign
		to.pos = from.pos + right * distance * 0.05 * k * sign
	elseif move == "orbit_right" or move == "orbit_left" then
		local sign = move == "orbit_right" and 1 or -1
		local turn = CFrame.Angles(0, math.rad(16 * k * sign), 0)
		to.pos = from.look + turn:VectorToWorldSpace(from.pos - from.look)
	elseif move == "rise" or move == "drop" then
		local sign = move == "rise" and 1 or -1
		to.pos = from.pos + Vector3.new(0, distance * 0.12 * k * sign, 0)
		to.look = from.look + Vector3.new(0, distance * 0.07 * k * sign, 0)
	elseif move == "tilt_up" or move == "tilt_down" then
		local sign = move == "tilt_up" and 1 or -1
		to.look = from.look + Vector3.new(0, distance * 0.22 * k * sign, 0)
	elseif move == "drift_right" or move == "drift_left" then
		local sign = move == "drift_right" and 1 or -1
		to.pos = from.pos + right * distance * 0.08 * k * sign
		to.look = from.look + right * distance * 0.08 * k * sign
	end
	return to
end

function M.plan(spec, ctx)
	local from = M.resolve(spec.from or spec, ctx)
	local to
	local move, shake, ease = spec.move or "push", spec.shake or 0, spec.ease or "linear"
	if move == "handheld" then
		shake = math.max(shake, spec.amount or 1)
	elseif move == "creep" and not spec.ease then
		ease = "in"
	end
	if spec.to then
		local target = spec.to
		if type(target) == "table" and type(spec.from) == "table" and not target.pos then
			local merged = table.clone(spec.from)
			for key, value in pairs(target) do
				merged[key] = value
			end
			target = merged
		end
		to = M.resolve(target, ctx)
	elseif move == "static" or move == "handheld" then
		to = from
	elseif move == "creep" then
		to = moved(from, "push", (spec.amount or 1) * 0.6)
	else
		to = moved(from, move, spec.amount)
	end
	local framing = type(spec.from) == "table" and spec.from or spec
	return { from = from, to = to, ease = ease, shake = shake, follow = spec.follow, roll = spec.dutch or framing.dutch or spec.roll or 0, hide = from.hide }
end

function M.at(plan, u, t, followDelta)
	local e = Util.ease(plan.ease, u)
	local pos = plan.from.pos:Lerp(plan.to.pos, e)
	local look = plan.from.look:Lerp(plan.to.look, e)
	if followDelta then
		pos = pos + followDelta
		look = look + followDelta
	end
	if plan.shake > 0 then
		local s = plan.shake * 0.04 * (look - pos).Magnitude
		look = look + Vector3.new(math.sin(t * 5.1) + math.sin(t * 8.3 + 1.2), math.sin(t * 6.7 + 0.5) + math.sin(t * 9.9), 0) * s * 0.5
	end
	return pos, look, Util.lerp(plan.from.fov, plan.to.fov, e)
end

function M.cframe(pos, look, rollDegrees)
	return CFrame.lookAt(pos, look) * CFrame.Angles(0, 0, math.rad(rollDegrees or 0))
end

return M
