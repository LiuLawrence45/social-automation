local Util = __require("util")

local M = {}
local BUILD = {}

local HAND = Enum.Font.PermanentMarker

local function model(parent, name)
	local m = Instance.new("Model")
	m.Name = name
	m.Parent = parent
	return m
end

local function part(m, size, cf, color, opts)
	opts = opts or {}
	local p = Util.part(m, { Size = Vector3.new(size[1], size[2], size[3]), CFrame = cf, Color = Util.color(color), Material = opts.material or Enum.Material.SmoothPlastic, Transparency = opts.transparency or 0 })
	if opts.shape then
		p.Shape = opts.shape
	end
	if opts.shadow == false then
		p.CastShadow = false
	end
	return p
end

local function text(p, face, value, opts)
	opts = opts or {}
	local gui = Util.surface(p, face, opts.ppi or 120)
	if opts.unlit then
		gui.LightInfluence = 0
	end
	local pad = opts.pad or 0.1
	return Util.label(gui, {
		Text = value, Font = opts.font or HAND, TextColor3 = Util.color(opts.color, Color3.fromRGB(35, 35, 60)),
		Size = UDim2.fromScale(1 - pad * 2, 1 - pad * 2), Position = UDim2.fromScale(pad, pad),
	})
end

function BUILD.note(m, spec)
	local w, h = spec.w or 1.1, spec.h or 1.1
	local paper = part(m, { w, h, 0.03 }, CFrame.new(), spec.color or "#fff3a8", { shadow = false })
	if spec.text then
		text(paper, Enum.NormalId.Front, spec.text, { color = spec.text_color, pad = 0.1 })
	end
	if spec.tape ~= false then
		part(m, { w * 0.34, h * 0.14, 0.035 }, CFrame.new(0, h * 0.47, 0) * CFrame.Angles(0, 0, math.rad(-4)), "#e9e2c8", { transparency = 0.25, shadow = false })
	end
end

function BUILD.sign(m, spec)
	local w, h = spec.w or 4, spec.h or 1.4
	local board = part(m, { w, h, 0.2 }, CFrame.new(), spec.color or "#ffffff")
	text(board, Enum.NormalId.Front, spec.text or "", { color = spec.text_color or "#222222", font = Enum.Font.FredokaOne, ppi = 60, pad = 0.08, unlit = spec.unlit })
	if spec.border then
		part(m, { w + 0.3, h + 0.3, 0.12 }, CFrame.new(0, 0, 0.06), spec.border)
	end
end

function BUILD.label(m, spec)
	local w, h = spec.w or 2.4, spec.h or 1.2
	local board = part(m, { w, h, 0.02 }, CFrame.new(), "#ffffff", { transparency = 1, shadow = false })
	local label = text(board, Enum.NormalId.Front, spec.text or "", { color = spec.text_color or "#ffffff", font = Enum.Font.FredokaOne, ppi = 80, pad = 0, unlit = true })
	local stroke = Instance.new("UIStroke")
	stroke.Thickness = 5
	stroke.Color = Color3.new(0, 0, 0)
	stroke.Parent = label
end

function BUILD.mug(m, spec)
	local color = spec.color or "#d9534f"
	part(m, { 0.6, 0.55, 0.55 }, CFrame.new(0, 0.3, 0) * CFrame.Angles(0, 0, math.rad(90)), color, { shape = Enum.PartType.Cylinder })
	part(m, { 0.04, 0.45, 0.45 }, CFrame.new(0, 0.59, 0) * CFrame.Angles(0, 0, math.rad(90)), "#4b2e1e", { shape = Enum.PartType.Cylinder })
	part(m, { 0.12, 0.34, 0.12 }, CFrame.new(0.36, 0.3, 0), color)
end

function BUILD.bag(m, spec)
	part(m, { 1.2, 1.4, 0.8 }, CFrame.new(0, 0.7, 0), spec.color or "#c9a06b")
	part(m, { 0.3, 0.9, 0.3 }, CFrame.new(-0.3, 1.55, 0) * CFrame.Angles(0, 0, math.rad(12)), "#e8c98a")
	part(m, { 0.5, 0.5, 0.5 }, CFrame.new(0.25, 1.5, 0.1), "#d6453d", { shape = Enum.PartType.Ball })
	part(m, { 0.5, 0.6, 0.4 }, CFrame.new(0.05, 1.55, -0.15), "#5aa84a")
end

function BUILD.flowers(m, spec)
	part(m, { 0.5, 0.9, 0.5 }, CFrame.new(0, 0.35, 0), spec.wrap or "#f7efe1")
	local colors = spec.colors or { "#ff6f91", "#ffd166", "#ff9770", "#f72585", "#ffffff" }
	local spots = { { 0, 1.15, 0 }, { -0.3, 1.0, 0.1 }, { 0.3, 1.0, -0.1 }, { 0.1, 1.05, 0.3 }, { -0.1, 1.0, -0.3 } }
	for index, spot in ipairs(spots) do
		part(m, { 0.44, 0.44, 0.44 }, CFrame.new(spot[1], spot[2], spot[3]), colors[(index - 1) % #colors + 1], { shape = Enum.PartType.Ball })
	end
	part(m, { 0.6, 0.3, 0.6 }, CFrame.new(0, 0.85, 0), "#4c9a4a")
end

function BUILD.box(m, spec)
	local s = spec.size or { 1.6, 1.3, 1.6 }
	part(m, s, CFrame.new(0, s[2] / 2, 0), spec.color or "#b98b55")
	part(m, { s[1] + 0.02, 0.12, 0.3 }, CFrame.new(0, s[2], 0), "#e3d2a6")
	if spec.text then
		local face = part(m, { s[1] * 0.9, s[2] * 0.6, 0.02 }, CFrame.new(0, s[2] / 2, -s[3] / 2 - 0.01), "#ffffff", { transparency = 1, shadow = false })
		text(face, Enum.NormalId.Front, spec.text, { color = "#3b2a18", pad = 0.02 })
	end
end

function BUILD.truck(m, spec)
	local body = part(m, { 5, 5, 9 }, CFrame.new(0, 4, 1.5), spec.color or "#f4f4f4")
	part(m, { 4.8, 3.2, 3 }, CFrame.new(0, 3.1, -4.4), spec.cab or "#e2574c")
	part(m, { 4.4, 1.3, 0.1 }, CFrame.new(0, 3.9, -5.92), "#9fd3ff", { material = Enum.Material.Glass })
	part(m, { 5.2, 0.6, 12.4 }, CFrame.new(0, 1.3, 0.2), "#333333")
	for _, z in ipairs({ -4.2, 3.6 }) do
		for _, x in ipairs({ -2.5, 2.5 }) do
			part(m, { 0.7, 1.9, 1.9 }, CFrame.new(x, 0.95, z), "#1c1c1c", { shape = Enum.PartType.Cylinder })
		end
	end
	for _, side in ipairs({ Enum.NormalId.Left, Enum.NormalId.Right }) do
		text(body, side, spec.text or "MOVERS", { color = "#e2574c", font = Enum.Font.FredokaOne, ppi = 40, pad = 0.12 })
	end
end

function BUILD.photo(m, spec, lib)
	local Rig = __require("rig")
	local Faces = __require("faces")
	part(m, { 1.3, 1.6, 0.12 }, CFrame.new(0, 0.8, 0.07), spec.frame or "#8a5a2b")
	part(m, { 1.05, 1.35, 0.04 }, CFrame.new(0, 0.8, 0), spec.back or "#bfe3ff", { shadow = false })
	part(m, { 0.5, 0.9, 0.08 }, CFrame.new(0, 0.3, 0.5) * CFrame.Angles(math.rad(-28), 0, 0), spec.frame or "#8a5a2b")
	local look = table.clone(lib.characters[spec.character or "young_man"] or {})
	look.scale = 0.2
	local rig = Rig.build(m, "Portrait", look)
	Rig.apply(rig, CFrame.new(0, -0.02, -0.14), { armL = { 0, 4 }, armR = { 0, 4 } })
	Faces.set(rig, lib.faces[spec.face or "calm"], false)
end

function BUILD.pen(m, spec)
	part(m, { 0.9, 0.1, 0.1 }, CFrame.new(), spec.color or "#2b4c9b")
	part(m, { 0.14, 0.08, 0.08 }, CFrame.new(-0.5, 0, 0), "#222222")
end

function BUILD.phone(m, spec)
	local body = part(m, { 0.55, 1.0, 0.08 }, CFrame.new(), "#1e1e1e")
	local screen = part(m, { 0.48, 0.9, 0.02 }, CFrame.new(0, 0, -0.04), spec.screen or "#dfefff", { shadow = false, material = Enum.Material.Neon })
	if spec.text then
		text(screen, Enum.NormalId.Front, spec.text, { color = "#111111", font = Enum.Font.GothamBold, pad = 0.06 })
	end
end

function BUILD.phone_chat(m, spec)
	part(m, { 0.74, 1.42, 0.08 }, CFrame.new(), "#15171c")
	local dark = spec.dark ~= false
	local screen = part(m, { 0.66, 1.32, 0.02 }, CFrame.new(0, 0, -0.04), dark and "#11151d" or "#eef2f7", { shadow = false, material = Enum.Material.Neon })
	local gui = Util.surface(screen, Enum.NormalId.Front, 400)
	gui.LightInfluence = 0
	local ink = dark and Color3.fromRGB(240, 243, 250) or Color3.fromRGB(20, 22, 28)
	Util.label(gui, { Text = spec.clock or "3:07", Font = Enum.Font.GothamMedium, TextColor3 = ink, Size = UDim2.fromOffset(70, 22), Position = UDim2.fromOffset(12, 8), TextXAlignment = Enum.TextXAlignment.Left })
	Util.label(gui, { Text = spec.name or "", Font = Enum.Font.GothamBold, TextColor3 = ink, Size = UDim2.fromOffset(240, 50), Position = UDim2.fromOffset(12, 40) })
	if spec.sub then
		Util.label(gui, { Text = spec.sub, Font = Enum.Font.GothamMedium, TextColor3 = Color3.fromRGB(150, 160, 178), Size = UDim2.fromOffset(240, 26), Position = UDim2.fromOffset(12, 92) })
	end
	local y = 136
	for _, message in ipairs(spec.messages or {}) do
		local lines = message.lines or 2
		local height = 26 + lines * 38
		local mine = message.me == true
		local bubble = Util.frame(gui, {
			Size = UDim2.fromOffset(mine and 190 or 232, height), Position = UDim2.fromOffset(mine and 62 or 12, y),
			BackgroundColor3 = mine and Color3.fromRGB(46, 120, 255) or (dark and Color3.fromRGB(44, 50, 64) or Color3.fromRGB(222, 226, 234)),
			Corner = UDim.new(0, 22),
		})
		Util.label(bubble, { Text = message.text, Font = Enum.Font.GothamBold, TextColor3 = mine and Color3.new(1, 1, 1) or ink, Size = UDim2.new(1, -24, 1, -16), Position = UDim2.fromOffset(12, 8) })
		y += height + 14
	end
	if spec.glow ~= false then
		local light = Instance.new("PointLight")
		light.Color = Color3.fromRGB(170, 200, 255)
		light.Brightness = spec.glow or 0.7
		light.Range = 7
		light.Shadows = true
		light.Parent = screen
	end
end

function BUILD.flashlight(m, spec)
	part(m, { 0.3, 0.3, 1.1 }, CFrame.new(), "#1c1c1f", { material = Enum.Material.Metal })
	-- glow: how bright the lens itself looks (1 a bare glowing lens, 0 a dark one); the beam is unchanged. A torch
	-- pointed towards the camera wants a low glow, or the lens blooms into a white blob.
	local glow = spec.glow == nil and 1 or math.clamp(spec.glow, 0, 1)
	local lens = part(m, { 0.36, 0.36, 0.1 }, CFrame.new(0, 0, -0.6), "#fff6d8", { material = glow >= 0.999 and Enum.Material.Neon or Enum.Material.SmoothPlastic, shadow = false })
	if glow < 0.999 then
		lens.Color = Color3.fromRGB(40, 38, 34):Lerp(Color3.fromRGB(255, 246, 216), glow)
	end
	local beam = Instance.new("SpotLight")
	beam.Face = Enum.NormalId.Front
	beam.Angle = spec.angle or 50
	beam.Range = spec.range or 34
	beam.Brightness = spec.brightness or 3
	beam.Color = Color3.fromRGB(255, 244, 214)
	beam.Shadows = true
	beam.Parent = lens
end

function BUILD.notes_grid(m, spec)
	local count, cols = spec.count or 12, spec.cols or 4
	local gap = spec.gap or 1.35
	local texts = spec.texts or {}
	local colors = spec.colors or { "#fff3a8", "#ffe0f0", "#dff5ff", "#e6ffd8" }
	local rows = math.ceil(count / cols)
	for index = 0, count - 1 do
		local col, row = index % cols, math.floor(index / cols)
		local x = (col - (cols - 1) / 2) * gap
		local y = ((rows - 1) / 2 - row) * gap
		local turn = (Util.hash("n" .. index) - 0.5) * (spec.jitter or 16)
		local paper = part(m, { 1.1, 1.1, 0.03 }, CFrame.new(x, y, 0) * CFrame.Angles(0, 0, math.rad(turn)), colors[index % #colors + 1], { shadow = false })
		if #texts > 0 then
			text(paper, Enum.NormalId.Front, texts[index % #texts + 1], { pad = 0.1 })
		end
	end
end

function BUILD.clock(m, spec)
	local body = part(m, { 2.2, 1.0, 0.3 }, CFrame.new(0, 0.5, 0), "#1c1c1c")
	local screen = part(m, { 2.0, 0.8, 0.02 }, CFrame.new(0, 0.5, -0.16), "#0b1a12", { shadow = false })
	text(screen, Enum.NormalId.Front, spec.text or "7:00", { color = spec.text_color or "#6bff8e", font = Enum.Font.Code, ppi = 80, pad = 0.05, unlit = true })
end

function BUILD.calendar(m, spec)
	local page = part(m, { 2.4, 2.8, 0.06 }, CFrame.new(), "#ffffff")
	local head = part(m, { 2.4, 0.7, 0.07 }, CFrame.new(0, 1.05, 0), spec.color or "#d9534f")
	text(head, Enum.NormalId.Front, spec.head or "FRIDAY", { color = "#ffffff", font = Enum.Font.FredokaOne, ppi = 80, pad = 0.08 })
	local body = part(m, { 2.2, 1.9, 0.02 }, CFrame.new(0, -0.3, -0.03), "#ffffff", { shadow = false })
	text(body, Enum.NormalId.Front, spec.text or "13", { color = "#222222", font = Enum.Font.FredokaOne, ppi = 80, pad = 0.05 })
end

function BUILD.suitcase(m, spec)
	part(m, { 1.4, 1.8, 0.7 }, CFrame.new(0, 0.9, 0), spec.color or "#3d6fb6")
	part(m, { 0.6, 0.25, 0.12 }, CFrame.new(0, 1.95, 0), "#222222")
end

function BUILD.book(m, spec)
	part(m, { 0.9, 0.16, 1.2 }, CFrame.new(), spec.color or "#7a4fbf")
	part(m, { 0.84, 0.1, 1.14 }, CFrame.new(0.03, 0, 0), "#f5f1e6")
end

function BUILD.notepad(m, spec)
	local pad = part(m, { 1.3, 0.08, 1.7 }, CFrame.new(), "#fffdf2")
	part(m, { 1.3, 0.1, 0.2 }, CFrame.new(0, 0.01, 0.75), "#c94c4c")
	if spec.text then
		text(pad, Enum.NormalId.Top, spec.text, { pad = 0.12 })
	end
end

function BUILD.heart(m, spec)
	local board = part(m, { 1.2, 1.2, 0.02 }, CFrame.new(), "#ffffff", { transparency = 1, shadow = false })
	text(board, Enum.NormalId.Front, spec.text or "♥", { color = spec.color or "#ff4d6d", font = Enum.Font.FredokaOne, ppi = 80, pad = 0, unlit = true })
end

function M.build(parent, spec, lib)
	local builder = BUILD[spec.prop]
	if not builder then
		error("unknown prop '" .. tostring(spec.prop) .. "'")
	end
	local m = model(parent, spec.prop)
	builder(m, spec, lib)
	m.WorldPivot = CFrame.new()
	if spec.scale and spec.scale ~= 1 then
		m:ScaleTo(spec.scale)
	end
	return m
end

function M.names()
	local names = {}
	for name in pairs(BUILD) do
		table.insert(names, name)
	end
	table.sort(names)
	return names
end

return M
