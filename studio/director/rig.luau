local Util = __require("util")
local Faces = __require("faces")

local M = {}

local LIMBS = { "Torso", "Head", "ArmL", "ArmR", "LegL", "LegR" }

local function accessory(model, limb, offset, props)
	local part = Util.part(model, props)
	part:SetAttribute("Limb", limb)
	part:SetAttribute("Offset", offset)
	return part
end

local HAIR = {}

function HAIR.short(add, c)
	add(Vector3.new(1.32, 0.42, 1.32), CFrame.new(0, 0.5, 0.02), c)
	add(Vector3.new(1.32, 0.75, 0.3), CFrame.new(0, 0.12, 0.54), c)
	add(Vector3.new(1.32, 0.2, 0.16), CFrame.new(0, 0.44, -0.6), c)
end

function HAIR.bun(add, c)
	add(Vector3.new(1.32, 0.4, 1.32), CFrame.new(0, 0.5, 0.02), c)
	add(Vector3.new(1.32, 0.7, 0.28), CFrame.new(0, 0.15, 0.54), c)
	add(Vector3.new(0.7, 0.7, 0.7), CFrame.new(0, 0.82, 0.3), c, Enum.PartType.Ball)
	add(Vector3.new(0.2, 0.5, 1.0), CFrame.new(0.62, 0.2, 0.1), c)
	add(Vector3.new(0.2, 0.5, 1.0), CFrame.new(-0.62, 0.2, 0.1), c)
end

function HAIR.ponytail(add, c)
	add(Vector3.new(1.32, 0.42, 1.32), CFrame.new(0, 0.5, 0.02), c)
	add(Vector3.new(1.32, 0.8, 0.3), CFrame.new(0, 0.12, 0.54), c)
	add(Vector3.new(0.4, 1.1, 0.4), CFrame.new(0, -0.1, 0.9) * CFrame.Angles(math.rad(-25), 0, 0), c)
	add(Vector3.new(1.32, 0.3, 0.16), CFrame.new(0, 0.4, -0.6), c)
end

function HAIR.long(add, c)
	add(Vector3.new(1.34, 0.42, 1.34), CFrame.new(0, 0.5, 0.02), c)
	add(Vector3.new(1.34, 1.6, 0.3), CFrame.new(0, -0.3, 0.56), c)
	add(Vector3.new(0.18, 1.3, 0.9), CFrame.new(0.66, -0.2, 0.12), c)
	add(Vector3.new(0.18, 1.3, 0.9), CFrame.new(-0.66, -0.2, 0.12), c)
	add(Vector3.new(1.34, 0.26, 0.16), CFrame.new(0, 0.42, -0.6), c)
end

function HAIR.balding(add, c)
	add(Vector3.new(0.18, 0.5, 1.0), CFrame.new(0.64, 0.1, 0.12), c)
	add(Vector3.new(0.18, 0.5, 1.0), CFrame.new(-0.64, 0.1, 0.12), c)
	add(Vector3.new(1.32, 0.5, 0.26), CFrame.new(0, 0.1, 0.56), c)
end

function HAIR.spiky(add, c)
	add(Vector3.new(1.32, 0.36, 1.32), CFrame.new(0, 0.48, 0.02), c)
	add(Vector3.new(1.32, 0.7, 0.3), CFrame.new(0, 0.14, 0.54), c)
	for index = -1, 1 do
		add(Vector3.new(0.34, 0.5, 0.34), CFrame.new(index * 0.4, 0.8, -0.2) * CFrame.Angles(math.rad(-20), 0, math.rad(index * -12)), c)
		add(Vector3.new(0.34, 0.5, 0.34), CFrame.new(index * 0.4, 0.82, 0.3) * CFrame.Angles(math.rad(15), 0, math.rad(index * -12)), c)
	end
end

function HAIR.cap(add, c, spec)
	local hat = Util.color(spec.hat_color, Color3.fromRGB(40, 90, 60))
	add(Vector3.new(1.34, 0.45, 1.34), CFrame.new(0, 0.52, 0.02), hat)
	add(Vector3.new(1.1, 0.1, 0.6), CFrame.new(0, 0.34, -0.85), hat)
	add(Vector3.new(1.32, 0.5, 0.26), CFrame.new(0, 0.05, 0.56), c)
end

function HAIR.bald() end

local function torsoFront(model, spec, scale)
	local style = spec.shirt_style or "plain"
	if style == "plain" then
		return
	end
	local plate = accessory(model, "Torso", CFrame.new(0, 0, -0.505 * scale), {
		Name = "ShirtFront", Size = Vector3.new(2, 2, 0.02) * scale, Transparency = 1, CastShadow = false,
	})
	local gui = Util.surface(plate, Enum.NormalId.Front, 100 / scale)
	local detail = Util.color(spec.detail_color, Color3.fromRGB(245, 245, 245))
	if style == "hoodie" then
		Util.frame(gui, { Size = UDim2.fromOffset(100, 46), Position = UDim2.fromOffset(50, 130), BackgroundColor3 = detail, BackgroundTransparency = 0.75, Corner = UDim.new(0, 10) })
		Util.frame(gui, { Size = UDim2.fromOffset(5, 44), Position = UDim2.fromOffset(84, 6), BackgroundColor3 = detail })
		Util.frame(gui, { Size = UDim2.fromOffset(5, 44), Position = UDim2.fromOffset(111, 6), BackgroundColor3 = detail })
	elseif style == "buttons" then
		Util.frame(gui, { Size = UDim2.fromOffset(4, 200), Position = UDim2.fromOffset(98, 0), BackgroundColor3 = detail, BackgroundTransparency = 0.5 })
		for index = 0, 3 do
			Util.frame(gui, { Size = UDim2.fromOffset(12, 12), Position = UDim2.fromOffset(94, 26 + index * 44), BackgroundColor3 = detail, Corner = UDim.new(1, 0) })
		end
	elseif style == "tie" then
		Util.frame(gui, { Size = UDim2.fromOffset(60, 200), Position = UDim2.fromOffset(70, 0), BackgroundColor3 = Color3.fromRGB(245, 245, 245) })
		Util.frame(gui, { Size = UDim2.fromOffset(20, 120), Position = UDim2.fromOffset(90, 6), BackgroundColor3 = detail, Corner = UDim.new(0, 6) })
	elseif style == "cardigan" then
		Util.frame(gui, { Size = UDim2.fromOffset(56, 200), Position = UDim2.fromOffset(72, 0), BackgroundColor3 = detail })
		for index = 0, 2 do
			Util.frame(gui, { Size = UDim2.fromOffset(10, 10), Position = UDim2.fromOffset(134, 60 + index * 44), BackgroundColor3 = Color3.fromRGB(250, 240, 200), Corner = UDim.new(1, 0) })
		end
	elseif style == "apron" then
		Util.frame(gui, { Size = UDim2.fromOffset(130, 170), Position = UDim2.fromOffset(35, 30), BackgroundColor3 = detail, Corner = UDim.new(0, 8) })
		Util.frame(gui, { Size = UDim2.fromOffset(60, 34), Position = UDim2.fromOffset(70, 110), BackgroundColor3 = Color3.new(0, 0, 0), BackgroundTransparency = 0.85, Corner = UDim.new(0, 6) })
	elseif style == "stripes" then
		for index = 0, 4 do
			Util.frame(gui, { Size = UDim2.fromOffset(200, 18), Position = UDim2.fromOffset(0, 12 + index * 40), BackgroundColor3 = detail })
		end
	elseif style == "badge" then
		Util.frame(gui, { Size = UDim2.fromOffset(44, 28), Position = UDim2.fromOffset(128, 40), BackgroundColor3 = detail, Corner = UDim.new(0, 4) })
	end
end

function M.build(parent, name, spec)
	local old = parent:FindFirstChild(name)
	if old then
		old:Destroy()
	end
	local scale = spec.scale or 1
	local skin = Util.color(spec.skin, Color3.fromRGB(245, 205, 150))
	local shirt = Util.color(spec.shirt, Color3.fromRGB(70, 120, 200))
	local pants = Util.color(spec.pants, Color3.fromRGB(50, 60, 90))
	local shoes = Util.color(spec.shoes, Color3.fromRGB(40, 40, 45))
	local flat = spec.silhouette and Util.color(spec.silhouette) or nil
	local model = Instance.new("Model")
	model.Name = name
	model:SetAttribute("Scale", scale)
	local function limb(limbName, size, color)
		return Util.part(model, { Name = limbName, Size = size * scale, Color = flat or color })
	end
	limb("Torso", Vector3.new(2, 2, 1), shirt)
	local head = limb("Head", Vector3.new(2, 1, 1), skin)
	local mesh = Instance.new("SpecialMesh")
	mesh.MeshType = Enum.MeshType.Head
	mesh.Scale = Vector3.new(1.25, 1.25, 1.25)
	mesh.Parent = head
	local sleeves = spec.sleeves ~= false
	limb("ArmL", Vector3.new(1, 2, 1), sleeves and shirt or skin)
	limb("ArmR", Vector3.new(1, 2, 1), sleeves and shirt or skin)
	limb("LegL", Vector3.new(1, 2, 1), pants)
	limb("LegR", Vector3.new(1, 2, 1), pants)
	for _, side in ipairs({ "L", "R" }) do
		if sleeves then
			accessory(model, "Arm" .. side, CFrame.new(0, -0.8 * scale, 0), { Name = "Hand", Size = Vector3.new(1.01, 0.42, 1.01) * scale, Color = flat or skin })
		else
			accessory(model, "Arm" .. side, CFrame.new(0, 0.72 * scale, 0), { Name = "Sleeve", Size = Vector3.new(1.02, 0.58, 1.02) * scale, Color = flat or shirt })
		end
		accessory(model, "Leg" .. side, CFrame.new(0, -0.86 * scale, -0.03 * scale), { Name = "Shoe", Size = Vector3.new(1.02, 0.3, 1.08) * scale, Color = flat or shoes })
	end
	if spec.skirt then
		accessory(model, "Torso", CFrame.new(0, -1.35 * scale, 0), { Name = "Skirt", Size = Vector3.new(2.1, 0.9, 1.1) * scale, Color = flat or Util.color(spec.skirt) })
	end
	local hairColor = flat or Util.color(spec.hair_color, Color3.fromRGB(70, 45, 30))
	local builder = HAIR[spec.hair or "short"] or HAIR.short
	builder(function(size, offset, color, shape)
		local part = accessory(model, "Head", CFrame.new(offset.Position * scale) * (offset - offset.Position), {
			Name = "Hair", Size = size * scale, Color = flat or color,
		})
		if shape then
			part.Shape = shape
		end
	end, hairColor, spec)
	if not flat then
		torsoFront(model, spec, scale)
		local plate = accessory(model, "Head", CFrame.new(0, 0, -0.632 * scale), {
			Name = "FacePlate", Size = Vector3.new(1.2, 1.2, 0.02) * scale, Transparency = 1, CastShadow = false,
		})
		Faces.build(plate, spec, scale)
	end
	model.Parent = parent
	return model
end

local function joint(values, mirror)
	values = values or {}
	local sign = mirror and -1 or 1
	return CFrame.Angles(math.rad(values[1] or 0), 0, 0)
		* CFrame.Angles(0, 0, math.rad((values[2] or 0) * sign))
		* CFrame.Angles(0, math.rad((values[3] or 0) * sign), 0)
end

function M.solve(rootCF, pose, scale)
	local root = pose.root or {}
	local pelvis = rootCF
		* CFrame.new((root[1] or 0) * scale, (2 + (root[2] or 0)) * scale, (root[3] or 0) * scale)
		* CFrame.Angles(0, math.rad(root[5] or 0), 0)
		* CFrame.Angles(math.rad(-(root[4] or 0)), 0, 0)
		* CFrame.Angles(0, 0, math.rad(root[6] or 0))
	local t = pose.torso or {}
	local torso = pelvis
		* CFrame.Angles(0, math.rad(t[2] or 0), 0)
		* CFrame.Angles(math.rad(-(t[1] or 0)), 0, 0)
		* CFrame.Angles(0, 0, math.rad(-(t[3] or 0)))
		* CFrame.new(0, 1 * scale, 0)
	local h = pose.head or {}
	local head = torso * CFrame.new(0, 1 * scale, 0)
		* CFrame.Angles(0, math.rad(h[2] or 0), 0)
		* CFrame.Angles(math.rad(h[1] or 0), 0, 0)
		* CFrame.Angles(0, 0, math.rad(h[3] or 0))
		* CFrame.new(0, 0.5 * scale, 0)
	local cfs = { Torso = torso, Head = head }
	cfs.ArmR = torso * CFrame.new(1.5 * scale, 0.5 * scale, 0) * joint(pose.armR, false) * CFrame.new(0, -0.5 * scale, 0)
	cfs.ArmL = torso * CFrame.new(-1.5 * scale, 0.5 * scale, 0) * joint(pose.armL, true) * CFrame.new(0, -0.5 * scale, 0)
	cfs.LegR = pelvis * CFrame.new(0.5 * scale, 0, 0) * joint(pose.legR, false) * CFrame.new(0, -1 * scale, 0)
	cfs.LegL = pelvis * CFrame.new(-0.5 * scale, 0, 0) * joint(pose.legL, true) * CFrame.new(0, -1 * scale, 0)
	return cfs
end

function M.apply(model, rootCF, pose)
	local scale = model:GetAttribute("Scale") or 1
	local cfs = M.solve(rootCF, pose, scale)
	for _, child in ipairs(model:GetChildren()) do
		if child:IsA("BasePart") then
			local limbName = child:GetAttribute("Limb")
			if limbName then
				child.CFrame = cfs[limbName] * child:GetAttribute("Offset")
			elseif cfs[child.Name] then
				child.CFrame = cfs[child.Name]
			end
		end
	end
	return cfs
end

function M.hand(cfs, side, scale)
	return cfs[side == "L" and "ArmL" or "ArmR"] * CFrame.new(0, -1.05 * scale, 0)
end

M.LIMBS = LIMBS

return M
