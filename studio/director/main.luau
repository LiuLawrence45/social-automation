local HttpService = game:GetService("HttpService")
local ServerStorage = game:GetService("ServerStorage")
local Util = __require("util")
local Rig = __require("rig")
local Avatar = __require("avatar")
local Props = __require("props")
local Sets = __require("sets")
local Stage = __require("stage")
local Capture = __require("capture")
local Judge = __require("judge")
local Posture = __require("posture")

local commands = {}
local cached = { key = nil, stage = nil, lit = nil }
local PARK = CFrame.new(0, -2000, 0)

local function root()
	return Util.folder(workspace, "Director")
end

function commands.prepare()
	for _, name in ipairs({ "Baseplate", "SpawnLocation" }) do
		local found = workspace:FindFirstChild(name)
		if found then
			found:Destroy()
		end
	end
	local lighting = game:GetService("Lighting")
	for _, child in ipairs(lighting:GetChildren()) do
		if (child:IsA("PostEffect") or child:IsA("Atmosphere")) and child.Name:sub(1, 9) ~= "Director_" then
			child:Destroy()
		end
	end
	if not lighting:FindFirstChild("Director_dof") then
		local dof = Instance.new("DepthOfFieldEffect")
		dof.Name = "Director_dof"
		dof.FarIntensity = 0.4
		dof.NearIntensity = 0
		dof.Enabled = false
		dof.Parent = lighting
	end
	local applied = {}
	local function try(name, change)
		local ok, problem = pcall(change)
		applied[name] = ok and true or tostring(problem)
	end
	try("lighting_style", function()
		lighting.LightingStyle = Enum.LightingStyle.Realistic
	end)
	try("prioritize_lighting_quality", function()
		lighting.PrioritizeLightingQuality = true
	end)
	try("edit_quality_level", function()
		settings().Rendering.EditQualityLevel = Enum.QualityLevel.Level21
	end)
	lighting.ShadowSoftness = 0.25
	root()
	return { prepared = true, quality = applied }
end

function commands.build_sets(args)
	local built = {}
	for index, name in ipairs(__SET_NAMES) do
		if not args.only or table.find(args.only, name) then
			local builder = Sets.begin(root(), name, index, nil, args.text)
			__require("set_" .. name)(builder)
			local model = builder:finish()
			model:SetAttribute("Source", args.sources and args.sources[name] or nil)
			built[name] = #model:GetDescendants()
		end
	end
	return built
end

-- Rebuilds each named set unless the one standing in the place was built from the same source (a hash from the pipeline).
function commands.ensure_sets(args)
	local folder = Util.folder(root(), "Sets")
	local built = {}
	for index, name in ipairs(__SET_NAMES) do
		local wanted = args.sets[name]
		local existing = folder:FindFirstChild(name)
		if wanted and (not existing or existing:GetAttribute("Source") ~= wanted) then
			local builder = Sets.begin(root(), name, index, nil, args.text)
			__require("set_" .. name)(builder)
			local model = builder:finish()
			model:SetAttribute("Source", wanted)
			built[name] = #model:GetDescendants()
		end
	end
	return { rebuilt = built }
end

function commands.build_cast(args, lib)
	local folder = Util.folder(root(), "Cast")
	local built = {}
	for role, spec in pairs(args.cast) do
		local look
		if type(spec) == "string" then
			look = lib.characters[spec]
		else
			look = table.clone(lib.characters[spec.character or ""] or {})
			for key, value in pairs(spec) do
				look[key] = value
			end
		end
		if not look then
			error("cast role '" .. role .. "' names an unknown character")
		end
		if look.rig == "avatar" then
			local signature = HttpService:JSONEncode(look)
			local existing = folder:FindFirstChild(role)
			if not existing or existing:GetAttribute("Look") ~= signature then
				local model = Avatar.build(folder, role, look)
				model:SetAttribute("Look", signature)
				if look.faceless then
					model:SetAttribute("Faceless", true)
				end
			end
		else
			local model = Rig.build(folder, role, look)
			Rig.apply(model, PARK, {})
		end
		table.insert(built, role)
	end
	return built
end

function commands.load(args, lib)
	local value = ServerStorage:FindFirstChild("DirectorTimeline")
	if not value then
		value = Instance.new("StringValue")
		value.Name = "DirectorTimeline"
		value.Parent = ServerStorage
	end
	value.Value = HttpService:JSONEncode(args.timeline)
	value:SetAttribute("Key", args.key or tostring(os.clock()))
	cached.key, cached.stage = nil, nil
	local cast = commands.build_cast({ cast = args.timeline.cast }, lib)
	return { shots = #args.timeline.shots, frames = args.timeline.frames, cast = cast }
end

local function stageFor(args, lib)
	local value = ServerStorage:FindFirstChild("DirectorTimeline")
	if not value then
		error("no timeline loaded; run the load command first")
	end
	local key = tostring(value:GetAttribute("Key")) .. "|" .. tostring(args.roll or 0)
	if cached.key ~= key or not cached.stage then
		cached.stage = Stage.new(HttpService:JSONDecode(value.Value), lib, { roll = args.roll })
		cached.key = key
	end
	return cached.stage
end

function commands.frame(args, lib)
	local stage = stageFor(args, lib)
	local before = stage.current and stage.current.index
	local info = stage:frame(args.frame)
	if args.marker then
		Capture.marker(args.frame)
	else
		Capture.marker(nil)
	end
	local settle = args.settle or 1
	if before ~= info.index then
		settle = math.max(settle, args.settle_cut or settle)
	end
	if stage.relit then
		stage.relit = false
		task.wait(args.settle_light or 1)
	end
	Capture.settle(settle)
	return info
end

-- Poses each frame, waits for it to be drawn and asks the capture side (args.sink) to take it.
-- The sink answers 409 while the frame marker on screen is not this frame's yet.
-- Studio computes a region's local lights about a second after the camera arrives there (settle_set)
-- and rebuilds sun shadows over several frames after the sun moves (settle_light).
function commands.capture(args, lib)
	local stage = stageFor(args, lib)
	local sent, waits = 0, 0
	local items = {}
	for _, frameIndex in ipairs(args.frames or {}) do
		table.insert(items, { frame = frameIndex, marker = frameIndex })
	end
	for _, variant in ipairs(args.variants or {}) do
		table.insert(items, variant)
	end
	for _, item in ipairs(items) do
		local frameIndex = item.marker
		if item.camera then
			stage.cameras = stage.cameras or {}
			stage.cameras[item.shot] = item.camera
			stage.current = nil
		end
		local before = stage.current and stage.current.index
		local info = stage:frame(item.frame)
		Capture.marker(frameIndex)
		local setName = stage.current.set.name
		if setName ~= cached.lit then
			cached.lit = setName
			stage.relit = false
			task.wait(args.settle_set or 2)
		elseif stage.relit then
			stage.relit = false
			task.wait(args.settle_light or 1)
		end
		Capture.settle(before ~= info.index and (args.settle_cut or 8) or (args.settle or 1))
		local tries = 0
		while true do
			local response = HttpService:RequestAsync({ Url = args.sink .. "/frame/" .. frameIndex, Method = "GET" })
			if response.StatusCode == 200 then
				break
			elseif response.StatusCode ~= 409 then
				error("capture failed at frame " .. frameIndex .. ": " .. response.Body)
			end
			tries += 1
			waits += 1
			if tries > 120 then
				error("frame " .. frameIndex .. " never showed in the Studio window (minimised, covered by a dialog, or on another desktop?)")
			end
			Capture.settle(1)
		end
		sent += 1
	end
	Capture.marker(nil)
	if args.variants then
		stage.cameras = nil
		stage.current = nil
	end
	return { frames = sent, waits = waits }
end

function commands.judge(args, lib)
	local stage = stageFor(args, lib)
	local positions = {}
	for position, shot in ipairs(stage.timeline.shots) do
		positions[shot.id] = position
	end
	local index = positions[args.shot]
	if not index then
		error("unknown shot '" .. tostring(args.shot) .. "'")
	end
	local results = {}
	for _, spec in ipairs(args.cameras) do
		local ok, result = pcall(Judge.check, stage, index, spec, args.previous)
		if ok then
			table.insert(results, result)
		else
			table.insert(results, { ok = false, issues = { "error:" .. tostring(result):sub(1, 160) }, metrics = {} })
		end
	end
	stage.cameras = nil
	stage.current = nil
	return results
end

function commands.posture(args, lib)
	local stage = stageFor(args, lib)
	for position, shot in ipairs(stage.timeline.shots) do
		if shot.id == args.shot then
			local result = Posture.check(stage, position, args.camera, args.samples or { 0, 0.25, 0.5, 0.75, 1 })
			stage.cameras = nil
			stage.current = nil
			return result
		end
	end
	error("unknown shot '" .. tostring(args.shot) .. "'")
end

function commands.calibrate(args)
	Capture.calibrate(args.on)
	Capture.marker(args.on and args.pattern or nil)
	Capture.settle(3)
	local viewport = workspace.CurrentCamera.ViewportSize
	return { viewport = { viewport.X, viewport.Y }, cell = Capture.CELL, bits = Capture.BITS, margin = Capture.MARGIN }
end

function commands.release()
	Capture.clear()
	cached.key, cached.stage, cached.lit = nil, nil, nil
	workspace.CurrentCamera.CameraType = Enum.CameraType.Fixed
	return {}
end

function commands.info(_, lib)
	local sets = {}
	for name, info in pairs(Sets.registry(root())) do
		local anchors, cameras, pieces = {}, {}, {}
		for anchor in pairs(info.anchors) do
			table.insert(anchors, anchor)
		end
		for cameraName in pairs(info.cameras) do
			table.insert(cameras, cameraName)
		end
		for piece in pairs(info.pieces) do
			table.insert(pieces, piece)
		end
		table.sort(anchors)
		table.sort(cameras)
		table.sort(pieces)
		sets[name] = { anchors = anchors, cameras = cameras, pieces = pieces, lighting = info.lighting }
	end
	local viewport = workspace.CurrentCamera.ViewportSize
	return { sets = sets, props = Props.names(), viewport = { viewport.X, viewport.Y } }
end

function commands.clear()
	local found = workspace:FindFirstChild("Director")
	if found then
		found:Destroy()
	end
	return {}
end

function commands.capture_service_probe()
	return Capture.serviceProbe()
end

return function(command, lib)
	local handler = commands[command.name]
	if not handler then
		error("unknown director command '" .. tostring(command.name) .. "'")
	end
	return handler(command.args or {}, lib)
end
