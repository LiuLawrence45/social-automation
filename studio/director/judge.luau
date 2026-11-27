local Camera = __require("camera")

local M = {}

local BAND = { 0.6, 0.78 }
local HEAD_SHARE = {
	extreme = { 0.2, 1.2 }, detail = { 0.14, 0.7 }, close = { 0.085, 0.3 }, medium = { 0.05, 0.16 },
	full = { 0.032, 0.1 }, wide = { 0.018, 0.07 }, vast = { 0.005, 0.04 },
}
local HARD = { head_out_of_frame = true, head_hidden = true, camera_inside = true, view_blocked = true, camera_off_set = true, subject_tiny = true }

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

local function solid(part)
	return part.Transparency < 0.6 and part.Name:sub(1, 6) ~= "Light_"
end

local function firstSolid(origin, target, ignore)
	local params = RaycastParams.new()
	params.FilterType = Enum.RaycastFilterType.Exclude
	local skip = table.clone(ignore)
	local direction = target - origin
	for _ = 1, 6 do
		params.FilterDescendantsInstances = skip
		local hit = workspace:Raycast(origin, direction, params)
		if not hit then
			return nil
		end
		if solid(hit.Instance) then
			return hit
		end
		table.insert(skip, hit.Instance)
	end
	return nil
end

local function inBand(x, y)
	return x and x > 0.1 and x < 0.9 and y > BAND[1] - 0.01 and y < BAND[2] + 0.01
end

function M.check(stage, index, spec, previous)
	local shot = stage.timeline.shots[index]
	stage.cameras = stage.cameras or {}
	stage.cameras[shot.id] = spec
	stage.current = nil
	local found, metrics = {}, {}
	local function issue(name, detail)
		found[name] = detail or true
	end
	local framing = type(spec.from) == "table" and spec.from or spec
	local subjectName = framing.on
	local last = math.max(shot.frame_start, shot.frame_end - 1)
	for step, u in ipairs({ 0, 0.5, 1 }) do
		stage:frame(shot.frame_start + math.floor((last - shot.frame_start) * u))
		local state = stage.current
		local view = state.view
		local held = { stage.propFolder }
		local subject = subjectName and state.actors[subjectName]
		local near = framing.ots and state.actors[framing.ots]
		if subject then
			local headSize = subject.avatar and subject.model.Head.Size.Y or 1.25 * subject.scale
			local x, y, depth = project(view, subject.head)
			if not x or x < 0.06 or x > 0.94 or y < 0.04 or y > 0.97 then
				issue("head_out_of_frame", subjectName)
			else
				if inBand(x, y) then
					issue("head_in_caption_band", subjectName)
				end
				local ignore = { subject.model, stage.propFolder }
				if near then
					table.insert(ignore, near.model)
				end
				local hit = firstSolid(view.pos, subject.head, ignore)
				if hit and hit.Distance < depth - 0.4 then
					issue("head_hidden", hit.Instance.Name)
				end
				local share = headSize / (2 * depth * math.tan(math.rad(view.fov / 2)) * Camera.ASPECT)
				if step == 2 then
					metrics.head = { math.floor(x * 1000) / 1000, math.floor(y * 1000) / 1000 }
					metrics.head_share = math.floor(share * 1000) / 1000
				end
				local range = not framing.ots and not framing.pov and HEAD_SHARE[framing.frame or "medium"]
				if share < 0.012 then
					issue("subject_tiny")
				elseif range and share < range[1] * 0.8 then
					issue("subject_too_small")
				elseif range and share > range[2] * 1.25 then
					issue("subject_too_large")
				end
			end
		elseif subjectName then
			local ok, place = pcall(function()
				return stage:place(subjectName)
			end)
			if ok then
				local x, y, depth = project(view, place.Position)
				if not x or x < 0.04 or x > 0.96 or y < 0.03 or y > 0.97 then
					issue("subject_out_of_frame", subjectName)
				end
			end
		end
		for who, entry in pairs(state.actors) do
			if who ~= subjectName and entry ~= near and not entry.hidden then
				local x, y = project(view, entry.head)
				if inBand(x, y) then
					issue("face_in_caption_band", who)
				end
			end
		end
		for _, item in ipairs(state.props) do
			if item.spec.text or item.spec.important then
				local x, y = project(view, item.model:GetPivot().Position)
				if inBand(x, y) then
					issue("text_in_caption_band", item.spec.prop)
				end
			end
		end
		local overlap = OverlapParams.new()
		overlap.FilterType = Enum.RaycastFilterType.Exclude
		overlap.FilterDescendantsInstances = held
		for _, part in ipairs(workspace:GetPartBoundsInRadius(view.pos, 0.45, overlap)) do
			if solid(part) and not (framing.pov and part:IsDescendantOf(stage.castFolder)) then
				issue("camera_inside", part.Name)
				break
			end
		end
		if not subject then
			local hit = firstSolid(view.pos, view.look, held)
			if hit and hit.Distance < (view.look - view.pos).Magnitude * 0.6 then
				issue("view_blocked", hit.Instance.Name)
			end
		end
		local below = RaycastParams.new()
		below.FilterType = Enum.RaycastFilterType.Include
		below.FilterDescendantsInstances = { state.set.model }
		if not workspace:Raycast(view.pos, Vector3.new(0, -60, 0), below) then
			issue("camera_off_set")
		end
		if step == 2 then
			local direction = (view.look - view.pos).Unit
			metrics.dir = { direction.X, direction.Y, direction.Z }
			metrics.on = subjectName
			metrics.distance = math.floor((view.look - view.pos).Magnitude * 10) / 10
		end
	end
	if previous and previous.on and previous.on == metrics.on and previous.dir and metrics.dir and previous.head_share and metrics.head_share then
		local a, b = Vector3.new(previous.dir[1], previous.dir[2], previous.dir[3]), Vector3.new(metrics.dir[1], metrics.dir[2], metrics.dir[3])
		local ratio = metrics.head_share / math.max(previous.head_share, 0.001)
		if math.deg(math.acos(math.clamp(a:Dot(b), -1, 1))) < 14 and ratio > 0.8 and ratio < 1.25 then
			issue("repeat_of_previous")
		end
	end
	local issues, ok = {}, true
	for name, detail in pairs(found) do
		table.insert(issues, detail == true and name or (name .. ":" .. tostring(detail)))
		if HARD[name] then
			ok = false
		end
	end
	table.sort(issues)
	return { ok = ok, issues = issues, metrics = metrics }
end

return M
