local Util = __require("util")

local M = {}

local INK = Color3.fromRGB(30, 26, 26)
local CENTER = 128
local EYE_DX = 43
local EYE_Y = 112

local function centered(frame, cx, cy, w, h)
	frame.AnchorPoint = Vector2.new(0.5, 0.5)
	frame.Position = UDim2.fromOffset(cx, cy)
	frame.Size = UDim2.fromOffset(w, h)
end

local function ring(parent, name, size, thickness, color)
	local circle = Util.frame(parent, { Name = name, BackgroundTransparency = 1, Size = UDim2.fromOffset(size, size), Corner = UDim.new(1, 0) })
	local stroke = Instance.new("UIStroke")
	stroke.Thickness = thickness
	stroke.Color = color
	stroke.Parent = circle
	return circle
end

function M.build(plate, spec, scale)
	local gui = Util.surface(plate, Enum.NormalId.Front, 256 / (1.2 * scale))
	gui.Name = "Face"
	local brow = spec.brow_color and Util.color(spec.brow_color) or INK
	for _, side in ipairs({ "L", "R" }) do
		local cx = CENTER + (side == "L" and -EYE_DX or EYE_DX)
		local eye = Util.frame(gui, { Name = "Eye" .. side, BackgroundColor3 = INK, Corner = UDim.new(1, 0) })
		centered(eye, cx, EYE_Y, 26, 38)
		Util.frame(eye, { Name = "Shine", BackgroundColor3 = Color3.new(1, 1, 1), Size = UDim2.fromScale(0.36, 0.26), Position = UDim2.fromScale(0.14, 0.12), Corner = UDim.new(1, 0) })
		local arc = Util.frame(gui, { Name = "Arc" .. side, BackgroundTransparency = 1, ClipsDescendants = true, Visible = false })
		centered(arc, cx, EYE_Y - 6, 44, 22)
		local arcRing = ring(arc, "Ring", 32, 6, INK)
		arcRing.Position = UDim2.fromOffset(6, 7)
		local browFrame = Util.frame(gui, { Name = "Brow" .. side, BackgroundColor3 = brow, Corner = UDim.new(1, 0) })
		centered(browFrame, cx, 76, 36, 7)
		local tear = Util.frame(gui, { Name = "Tear" .. side, BackgroundColor3 = Color3.fromRGB(110, 190, 255), Corner = UDim.new(1, 0), Visible = false })
		centered(tear, cx + (side == "L" and -4 or 4), 150, 11, 30)
		local blush = Util.frame(gui, { Name = "Blush" .. side, BackgroundColor3 = Color3.fromRGB(255, 120, 130), BackgroundTransparency = 0.45, Corner = UDim.new(1, 0), Visible = false })
		centered(blush, cx + (side == "L" and -14 or 14), 148, 30, 16)
		if spec.glasses then
			local lens = Util.frame(gui, { Name = "Lens" .. side, BackgroundColor3 = Color3.new(1, 1, 1), BackgroundTransparency = 0.8, Corner = UDim.new(0.3, 0), ZIndex = 3 })
			centered(lens, cx, EYE_Y, 58, 50)
			local stroke = Instance.new("UIStroke")
			stroke.Thickness = 5
			stroke.Color = Util.color(spec.glasses_color, Color3.fromRGB(60, 50, 50))
			stroke.Parent = lens
		end
	end
	if spec.glasses then
		local bridge = Util.frame(gui, { Name = "Bridge", BackgroundColor3 = Util.color(spec.glasses_color, Color3.fromRGB(60, 50, 50)), ZIndex = 3 })
		centered(bridge, CENTER, EYE_Y - 4, 30, 5)
	end
	if spec.moustache then
		local moustache = Util.frame(gui, { Name = "Moustache", BackgroundColor3 = Util.color(spec.moustache), Corner = UDim.new(0.5, 0), ZIndex = 2 })
		centered(moustache, CENTER, 154, 74, 18)
	end
	local smile = Util.frame(gui, { Name = "MouthSmile", BackgroundTransparency = 1, ClipsDescendants = true })
	centered(smile, CENTER, 184, 70, 30)
	ring(smile, "Ring", 52, 6, INK).Position = UDim2.fromOffset(9, -28)
	local frown = Util.frame(gui, { Name = "MouthFrown", BackgroundTransparency = 1, ClipsDescendants = true, Visible = false })
	centered(frown, CENTER, 184, 66, 26)
	ring(frown, "Ring", 46, 6, INK).Position = UDim2.fromOffset(10, 8)
	local grin = Util.frame(gui, { Name = "MouthGrin", BackgroundTransparency = 1, ClipsDescendants = true, Visible = false })
	centered(grin, CENTER, 186, 64, 34)
	local cavity = Util.frame(grin, { BackgroundColor3 = Color3.fromRGB(90, 30, 35), Size = UDim2.fromOffset(64, 64), Position = UDim2.fromOffset(0, -30), Corner = UDim.new(1, 0) })
	Util.frame(cavity, { BackgroundColor3 = Color3.fromRGB(240, 110, 120), Size = UDim2.fromOffset(34, 26), Position = UDim2.fromOffset(15, 44), Corner = UDim.new(1, 0) })
	local o = Util.frame(gui, { Name = "MouthO", BackgroundColor3 = Color3.fromRGB(90, 30, 35), Corner = UDim.new(1, 0), Visible = false })
	centered(o, CENTER, 186, 24, 30)
	local line = Util.frame(gui, { Name = "MouthLine", BackgroundColor3 = INK, Corner = UDim.new(1, 0), Visible = false })
	centered(line, CENTER, 184, 40, 6)
	return gui
end

local EYES = {
	open = { 26, 38 },
	wide = { 32, 46 },
	closed = { 30, 6 },
	squint = { 28, 14 },
	half = { 26, 22 },
}

function M.set(model, face, blink)
	local plate = model:FindFirstChild("FacePlate")
	local gui = plate and plate:FindFirstChild("Face")
	if not gui then
		return
	end
	local eyes = face.eyes or "open"
	if blink and eyes ~= "happy" then
		eyes = "closed"
	end
	local brows = face.brows or { 0, 0, 0 }
	for _, side in ipairs({ "L", "R" }) do
		local cx = CENTER + (side == "L" and -EYE_DX or EYE_DX)
		local eye = gui["Eye" .. side]
		local arc = gui["Arc" .. side]
		arc.Visible = eyes == "happy"
		eye.Visible = eyes ~= "happy"
		local size = EYES[eyes] or EYES.open
		eye.Size = UDim2.fromOffset(size[1], size[2])
		eye.Shine.Visible = size[2] > 20
		local browFrame = gui["Brow" .. side]
		local angle = side == "L" and brows[1] or brows[2]
		browFrame.Rotation = side == "L" and -angle or angle
		browFrame.Position = UDim2.fromOffset(cx, 76 + (brows[3] or 0))
		browFrame.Visible = face.brows ~= false
		gui["Tear" .. side].Visible = face.tears == true
		gui["Blush" .. side].Visible = face.blush == true
	end
	local mouth = face.mouth or "smile"
	gui.MouthSmile.Visible = mouth == "smile"
	gui.MouthFrown.Visible = mouth == "frown"
	gui.MouthGrin.Visible = mouth == "grin"
	gui.MouthO.Visible = mouth == "o"
	gui.MouthLine.Visible = mouth == "line" or mouth == "small"
	gui.MouthLine.Size = UDim2.fromOffset(mouth == "small" and 20 or 40, 6)
end

return M
