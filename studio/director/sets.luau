local Util = __require("util")
local HttpService = game:GetService("HttpService")

local M = {}
local B = {}
B.__index = B

local SPACING = 600
-- Set files give light brightness on a scale where 1 is a normal room lamp; measured in Studio, 0.3 keeps walls from clipping to white.
local LIGHT_SCALE = 0.3
local PARK = CFrame.new(0, -3000, 0)
local TRIM = "#fbf6ea"

function M.begin(root, name, index, meta)
	local folder = Util.folder(root, "Sets")
	local old = folder:FindFirstChild(name)
	if old then
		old:Destroy()
	end
	local model = Instance.new("Model")
	model.Name = name
	local self = setmetatable({}, B)
	self.model = model
	self.parent = model
	self.origin = CFrame.new(index * SPACING, 0, 0)
	self.info = { name = name, anchors = {}, cameras = {}, pieces = {}, lighting = meta and meta.lighting or "day" }
	self.folder = folder
	return self
end

function B:box(size, pos, color, opts)
	opts = opts or {}
	local cf = self.origin * CFrame.new(pos[1], pos[2], pos[3])
	if opts.rot then
		cf = cf * CFrame.Angles(math.rad(opts.rot[1] or 0), math.rad(opts.rot[2] or 0), math.rad(opts.rot[3] or 0))
	end
	local part = Util.part(opts.parent or self.parent, {
		Name = opts.name or "Part", Size = Vector3.new(size[1], size[2], size[3]), CFrame = cf,
		Color = Util.color(color), Material = opts.material or Enum.Material.SmoothPlastic,
		Transparency = opts.transparency or 0, Reflectance = opts.reflectance or 0,
	})
	if opts.shape then
		part.Shape = opts.shape
	end
	if opts.shadow == false then
		part.CastShadow = false
	end
	return part
end

function B:floor(x0, x1, z0, z1, color, opts)
	return self:box({ x1 - x0, 1, z1 - z0 }, { (x0 + x1) / 2, -0.5, (z0 + z1) / 2 }, color, opts)
end

function B:ceiling(x0, x1, z0, z1, y, color)
	return self:box({ x1 - x0, 0.5, z1 - z0 }, { (x0 + x1) / 2, y + 0.25, (z0 + z1) / 2 }, color, { shadow = false, material = Enum.Material.Plaster })
end

-- Walls and ceilings cast no shadow, so the key light (the sun, aimed from the camera side for every shot)
-- reaches into a room while furniture and characters still throw real shadows on the floor and walls.
local function shell(opts)
	local merged = table.clone(opts or {})
	if merged.shadow == nil then
		merged.shadow = false
	end
	merged.material = merged.material or Enum.Material.Plaster
	return merged
end

function B:baseboard(size, pos)
	return self:box(size, pos, TRIM, { shadow = false })
end

function B:wallX(x0, x1, z, height, color, opts)
	opts = shell(opts)
	local thick, base = opts.thick or 0.4, opts.base or 0
	if base == 0 and height >= 6 and opts.trim ~= false then
		self:baseboard({ x1 - x0, 0.45, thick + 0.12 }, { (x0 + x1) / 2, 0.225, z })
	end
	return self:box({ x1 - x0, height, thick }, { (x0 + x1) / 2, base + height / 2, z }, color, opts)
end

function B:wallZ(z0, z1, x, height, color, opts)
	opts = shell(opts)
	local thick, base = opts.thick or 0.4, opts.base or 0
	if base == 0 and height >= 6 and opts.trim ~= false then
		self:baseboard({ thick + 0.12, 0.45, z1 - z0 }, { x, 0.225, (z0 + z1) / 2 })
	end
	return self:box({ thick, height, z1 - z0 }, { x, base + height / 2, (z0 + z1) / 2 }, color, opts)
end

function B:text(part, face, text, opts)
	opts = opts or {}
	local gui = Util.surface(part, face, opts.ppi or 60)
	if opts.unlit then
		gui.LightInfluence = 0
	end
	local label = Util.label(gui, {
		Text = text, TextColor3 = Util.color(opts.color, Color3.new(0, 0, 0)), Font = opts.font or Enum.Font.FredokaOne,
		Size = UDim2.fromScale(opts.w or 0.9, opts.h or 0.8), Position = UDim2.fromScale(0.5, 0.5), AnchorPoint = Vector2.new(0.5, 0.5),
	})
	return label
end

function B:sign(text, size, pos, color, textColor, opts)
	opts = opts or {}
	local part = self:box(size, pos, color, opts)
	self:text(part, opts.face or Enum.NormalId.Back, text, { color = textColor, font = opts.font, ppi = opts.ppi, unlit = opts.unlit })
	return part
end

function B:light(pos, color, brightness, range)
	local holder = self:box({ 0.2, 0.2, 0.2 }, pos, "#ffffff", { transparency = 1, name = "Light", shadow = false })
	local light = Instance.new("PointLight")
	light.Color = Util.color(color)
	light.Brightness = (brightness or 1) * LIGHT_SCALE
	light.Range = range or 16
	light.Shadows = false
	light.Parent = holder
	return light
end

function B:anchor(name, x, y, z, yaw)
	self.info.anchors[name] = { pos = { x, y, z }, yaw = yaw or 0 }
end

function B:camera(name, pos, look, fov)
	self.info.cameras[name] = { pos = pos, look = look, fov = fov }
end

function B:piece(name, kind, pivot, axis, build, hide)
	local sub = Instance.new("Model")
	sub.Name = "Piece_" .. name
	local previous = self.parent
	self.parent = sub
	build(self)
	self.parent = previous
	sub.WorldPivot = self.origin * CFrame.new(pivot[1], pivot[2], pivot[3])
	sub.Parent = self.model
	self.info.pieces[name] = { name = name, kind = kind, pivot = pivot, axis = axis or { 0, 1, 0 }, hide = hide }
end

function B:finish()
	self.model:SetAttribute("Origin", self.origin)
	self.model:SetAttribute("Info", HttpService:JSONEncode(self.info))
	self.model.Parent = self.folder
	return self.model
end

function M.registry(root)
	local found = {}
	local folder = root:FindFirstChild("Sets")
	if not folder then
		return found
	end
	for _, model in ipairs(folder:GetChildren()) do
		local raw = model:GetAttribute("Info")
		if raw then
			local info = HttpService:JSONDecode(raw)
			info.origin = model:GetAttribute("Origin")
			info.model = model
			found[info.name] = info
		end
	end
	return found
end

function M.pieceCFrame(set, piece, value)
	local base = set.origin * CFrame.new(piece.pivot[1], piece.pivot[2], piece.pivot[3])
	local axis = Vector3.new(piece.axis[1], piece.axis[2], piece.axis[3])
	if piece.kind == "hinge" then
		return base * CFrame.fromAxisAngle(axis, math.rad(value))
	elseif piece.kind == "slide" then
		return base * CFrame.new(axis * value)
	end
	return base
end

function M.movePiece(set, piece, value, cameraPos)
	local model = set.model:FindFirstChild("Piece_" .. piece.name)
	if not model then
		return
	end
	local hide = piece.hide
	if hide and cameraPos then
		local position = set.origin:PointToObjectSpace(cameraPos)
		local coordinate = hide.axis == "x" and position.X or position.Z
		if (coordinate - hide.at) * hide.side > 0 then
			model:PivotTo(PARK)
			return
		end
	end
	model:PivotTo(M.pieceCFrame(set, piece, value))
end

M.SPACING = SPACING

return M
