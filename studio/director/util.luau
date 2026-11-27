local M = {}

function M.clamp(value, low, high)
	return math.max(low, math.min(high, value))
end

function M.lerp(a, b, u)
	return a + (b - a) * u
end

function M.ease(kind, u)
	u = M.clamp(u, 0, 1)
	if kind == "inout" then
		return u * u * (3 - 2 * u)
	elseif kind == "out" then
		return 1 - (1 - u) * (1 - u)
	elseif kind == "in" then
		return u * u
	elseif kind == "back" then
		local s = 1.70158
		local v = u - 1
		return v * v * ((s + 1) * v + s) + 1
	end
	return u
end

function M.hash(text)
	local h = 2166136261
	for index = 1, #text do
		h = bit32.bxor(h, string.byte(text, index))
		h = (h * 16777619) % 4294967296
	end
	return h / 4294967296
end

function M.color(value, fallback)
	if typeof(value) == "Color3" then
		return value
	elseif type(value) == "string" and #value == 7 then
		return Color3.fromHex(value)
	elseif type(value) == "table" then
		return Color3.fromRGB(value[1], value[2], value[3])
	end
	return fallback or Color3.fromRGB(200, 200, 200)
end

function M.vec(value)
	if typeof(value) == "Vector3" then
		return value
	end
	return Vector3.new(value[1] or 0, value[2] or 0, value[3] or 0)
end

function M.yaw(degrees)
	return CFrame.Angles(0, math.rad(degrees), 0)
end

function M.part(parent, props)
	local part = Instance.new("Part")
	part.Anchored = true
	part.CanCollide = false
	part.Material = Enum.Material.SmoothPlastic
	part.TopSurface = Enum.SurfaceType.Smooth
	part.BottomSurface = Enum.SurfaceType.Smooth
	part.CastShadow = true
	for key, value in pairs(props) do
		part[key] = value
	end
	part.Parent = parent
	return part
end

function M.folder(parent, name)
	local found = parent:FindFirstChild(name)
	if found then
		return found
	end
	local folder = Instance.new("Folder")
	folder.Name = name
	folder.Parent = parent
	return folder
end

function M.label(parent, props)
	local label = Instance.new("TextLabel")
	label.BackgroundTransparency = 1
	label.Size = UDim2.fromScale(1, 1)
	label.TextScaled = true
	label.TextWrapped = true
	label.Font = Enum.Font.FredokaOne
	label.TextColor3 = Color3.new(0, 0, 0)
	for key, value in pairs(props) do
		label[key] = value
	end
	label.Parent = parent
	return label
end

function M.surface(part, face, pixelsPerStud)
	local gui = Instance.new("SurfaceGui")
	gui.Face = face or Enum.NormalId.Front
	gui.SizingMode = Enum.SurfaceGuiSizingMode.PixelsPerStud
	gui.PixelsPerStud = pixelsPerStud or 100
	gui.LightInfluence = 1
	gui.Parent = part
	return gui
end

function M.frame(parent, props)
	local frame = Instance.new("Frame")
	frame.BorderSizePixel = 0
	frame.BackgroundColor3 = Color3.new(0, 0, 0)
	local corner = props.Corner
	props.Corner = nil
	for key, value in pairs(props) do
		frame[key] = value
	end
	if corner then
		local round = Instance.new("UICorner")
		round.CornerRadius = corner
		round.Parent = frame
	end
	frame.Parent = parent
	return frame
end

return M
