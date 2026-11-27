local Players = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local ClipProvider = game:GetService("AnimationClipProvider")
local HttpService = game:GetService("HttpService")
local Util = __require("util")
local Pose = __require("pose")

local M = {}

local PARENT = {
	LowerTorso = "HumanoidRootPart", UpperTorso = "LowerTorso", Head = "UpperTorso",
	LeftUpperArm = "UpperTorso", LeftLowerArm = "LeftUpperArm", LeftHand = "LeftLowerArm",
	RightUpperArm = "UpperTorso", RightLowerArm = "RightUpperArm", RightHand = "RightLowerArm",
	LeftUpperLeg = "LowerTorso", LeftLowerLeg = "LeftUpperLeg", LeftFoot = "LeftLowerLeg",
	RightUpperLeg = "LowerTorso", RightLowerLeg = "RightUpperLeg", RightFoot = "RightLowerLeg",
}
local ORDER = {
	"LowerTorso", "UpperTorso", "Head",
	"LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand",
	"LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "RightUpperLeg", "RightLowerLeg", "RightFoot",
}
local WEAR = {
	hair = Enum.AccessoryType.Hair, hat = Enum.AccessoryType.Hat, face = Enum.AccessoryType.Face,
	jacket = Enum.AccessoryType.Jacket, sweater = Enum.AccessoryType.Sweater, shirt = Enum.AccessoryType.Shirt,
	tshirt = Enum.AccessoryType.TShirt, pants = Enum.AccessoryType.Pants, shorts = Enum.AccessoryType.Shorts,
	skirt = Enum.AccessoryType.DressSkirt,
}
local CLASSIC = { classic_shirt = "Shirt", classic_pants = "Pants", graphic = "GraphicTShirt", head = "Head", face_decal = "Face" }
local SCALES = { height = "HeightScale", width = "WidthScale", depth = "DepthScale", head_size = "HeadScale", proportion = "ProportionScale", body_type = "BodyTypeScale" }
local SKIN = { "HeadColor", "TorsoColor", "LeftArmColor", "RightArmColor", "LeftLegColor", "RightLegColor" }
local PARK = CFrame.new(0, -2000, 0)

local rigs = setmetatable({}, { __mode = "k" })
local clips = {}

local function outfit(bundle)
	for _, item in ipairs(AssetService:GetBundleDetailsAsync(bundle).Items) do
		if item.Type == "UserOutfit" then
			return Players:GetHumanoidDescriptionFromOutfitId(item.Id)
		end
	end
	return nil
end

local function describe(spec)
	local description = spec.bundle and outfit(spec.bundle) or Instance.new("HumanoidDescription")
	if spec.wear then
		local worn = {}
		for index, item in ipairs(spec.wear) do
			local kind = WEAR[item.type]
			if not kind then
				error("unknown wear type '" .. tostring(item.type) .. "'")
			end
			table.insert(worn, { AssetId = item.id, AccessoryType = kind, Order = item.order or index })
		end
		description:SetAccessories(worn, true)
	end
	-- Some bundles ship a head with no face controls, and no face set on it can show. head_bundle takes the head
	-- (with its brows and lashes) of another bundle and keeps this one's body, hair and clothes.
	if spec.head_bundle then
		local donor = outfit(spec.head_bundle)
		if not donor then
			error("head_bundle " .. tostring(spec.head_bundle) .. " has no outfit")
		end
		description.Head = donor.Head
		local worn = {}
		for _, item in ipairs(description:GetAccessories(true)) do
			if item.AccessoryType ~= Enum.AccessoryType.Eyebrow and item.AccessoryType ~= Enum.AccessoryType.Eyelash then
				table.insert(worn, item)
			end
		end
		for _, item in ipairs(donor:GetAccessories(true)) do
			if item.AccessoryType == Enum.AccessoryType.Eyebrow or item.AccessoryType == Enum.AccessoryType.Eyelash then
				table.insert(worn, item)
			end
		end
		description:SetAccessories(worn, true)
	end
	for key, property in pairs(CLASSIC) do
		if spec[key] then
			description[property] = spec[key]
		end
	end
	for key, property in pairs(SCALES) do
		if spec[key] then
			description[property] = spec[key]
		end
	end
	if spec.skin then
		for _, property in ipairs(SKIN) do
			description[property] = Util.color(spec.skin)
		end
	end
	-- Bare legs painted one colour read as long trousers under shorts or a skirt.
	if spec.legs then
		description.LeftLegColor = Util.color(spec.legs)
		description.RightLegColor = Util.color(spec.legs)
	end
	return description
end

local function silhouette(model, color)
	for _, item in ipairs(model:GetDescendants()) do
		if item:IsA("SurfaceAppearance") or item:IsA("Decal") or item:IsA("Clothing") or item:IsA("ShirtGraphic") then
			item:Destroy()
		elseif item:IsA("MeshPart") then
			item.TextureID = ""
			item.Color = color
			item.Material = Enum.Material.SmoothPlastic
		elseif item:IsA("BasePart") then
			item.Color = color
		elseif item:IsA("SpecialMesh") then
			item.TextureId = ""
		end
	end
end

local function eyes(model, spec)
	local head = model.Head
	local unit = head.Size.Y
	for _, side in ipairs({ -1, 1 }) do
		local eye = Instance.new("Part")
		eye.Name = "Eye"
		eye.Shape = Enum.PartType.Ball
		eye.Size = Vector3.one * unit * (spec.size or 0.13)
		eye.Material = Enum.Material.Neon
		eye.Color = Util.color(spec.color, Color3.new(1, 1, 1))
		eye.CanCollide = false
		eye.Massless = true
		eye.CastShadow = false
		eye.Parent = head
		local weld = Instance.new("Weld")
		weld.Part0 = head
		weld.Part1 = eye
		weld.C0 = CFrame.new(side * unit * (spec.spread or 0.2), unit * (spec.up or 0.08), -head.Size.Z * (spec.forward or 0.47))
		weld.Parent = eye
	end
end

local function fixed(model, to, name, size, offset, color, material)
	local piece = Instance.new("Part")
	piece.Name = name
	piece.Size = size
	piece.Color = color
	piece.Material = material or Enum.Material.SmoothPlastic
	piece.CanCollide = false
	piece.CanQuery = false
	piece.Massless = true
	piece.CastShadow = false
	piece.Parent = to
	local weld = Instance.new("Weld")
	weld.Part0 = to
	weld.Part1 = piece
	weld.C0 = offset
	weld.Parent = piece
	return piece
end

-- A dark vest over the chest with a word across it, a badge and a belt: what makes two people in ordinary clothes
-- read as officers. Sized from the body it is put on.
local function vest(model, spec)
	local torso, hips = model.UpperTorso, model.LowerTorso
	local color = Util.color(spec.color, Color3.fromRGB(20, 28, 51))
	local size = Vector3.new(torso.Size.X * 0.74, torso.Size.Y * 0.8, torso.Size.Z * 0.84)
	local shell = fixed(model, torso, "Vest", size, CFrame.new(0, -torso.Size.Y * 0.06, 0), color, Enum.Material.Fabric)
	for _, face in ipairs({ Enum.NormalId.Front, Enum.NormalId.Back }) do
		local gui = Util.surface(shell, face, 120)
		local label = Instance.new("TextLabel")
		label.BackgroundTransparency = 1
		label.Size = face == Enum.NormalId.Back and UDim2.fromScale(0.86, 0.3) or UDim2.fromScale(0.4, 0.16)
		label.Position = face == Enum.NormalId.Back and UDim2.fromScale(0.07, 0.12) or UDim2.fromScale(0.06, 0.14)
		label.Text = spec.text or "POLICE"
		label.TextScaled = true
		label.Font = Enum.Font.GothamBold
		label.TextColor3 = Color3.fromRGB(235, 238, 245)
		label.Parent = gui
	end
	fixed(model, torso, "Badge", Vector3.new(size.X * 0.14, size.X * 0.16, 0.04), CFrame.new(-size.X * 0.26, size.Y * 0.2, -size.Z / 2 - 0.01), Color3.fromRGB(214, 176, 72), Enum.Material.Metal)
	fixed(model, hips, "Belt", Vector3.new(hips.Size.X * 1.04, hips.Size.Y * 0.28, hips.Size.Z * 1.06), CFrame.new(0, hips.Size.Y * 0.3, 0), Color3.fromRGB(18, 18, 20), Enum.Material.Leather)
	fixed(model, hips, "Buckle", Vector3.new(hips.Size.X * 0.16, hips.Size.Y * 0.24, 0.05), CFrame.new(0, hips.Size.Y * 0.3, -hips.Size.Z * 0.53 - 0.02), Color3.fromRGB(170, 172, 178), Enum.Material.Metal)
end

-- A call-taker's headset: a band over the head, a cup on one ear and a microphone boom to the mouth.
local function headset(model, spec)
	local head = model.Head
	local w, h, d = head.Size.X, head.Size.Y, head.Size.Z
	local dark = Color3.fromRGB(24, 24, 28)
	local side = spec.side == "R" and 1 or -1
	fixed(model, head, "HeadsetBand", Vector3.new(w * 1.12, h * 0.07, d * 0.16), CFrame.new(0, h * 0.56, d * 0.04), dark)
	for _, s in ipairs({ -1, 1 }) do
		fixed(model, head, "HeadsetArm", Vector3.new(w * 0.06, h * 0.5, d * 0.14), CFrame.new(s * w * 0.56, h * 0.3, d * 0.04), dark)
	end
	local cup = fixed(model, head, "HeadsetCup", Vector3.new(w * 0.12, h * 0.34, d * 0.4), CFrame.new(side * w * 0.58, h * 0.02, d * 0.04), dark)
	cup.Shape = Enum.PartType.Block
	fixed(model, head, "HeadsetPad", Vector3.new(w * 0.1, h * 0.2, d * 0.24), CFrame.new(-side * w * 0.57, h * 0.06, d * 0.04), dark)
	local boom = fixed(model, head, "HeadsetBoom", Vector3.new(w * 0.05, h * 0.05, d * 0.62), CFrame.new(side * w * 0.56, -h * 0.2, -d * 0.3) * CFrame.Angles(0, math.rad(side * 18), 0), dark)
	fixed(model, boom, "HeadsetMic", Vector3.new(w * 0.1, h * 0.09, d * 0.12), CFrame.new(0, 0, -d * 0.34), Color3.fromRGB(46, 46, 52))
end

function M.build(parent, name, spec)
	local old = parent:FindFirstChild(name)
	if old then
		old:Destroy()
	end
	local model = Players:CreateHumanoidModelFromDescription(describe(spec), Enum.HumanoidRigType.R15)
	model.Name = name
	for _, child in ipairs(model:GetChildren()) do
		if child:IsA("LuaSourceContainer") then
			child:Destroy()
		end
	end
	local humanoid = model:FindFirstChildOfClass("Humanoid")
	humanoid.DisplayDistanceType = Enum.HumanoidDisplayDistanceType.None
	humanoid.HealthDisplayType = Enum.HumanoidHealthDisplayType.AlwaysOff
	if spec.size then
		model:ScaleTo(spec.size)
	end
	local root = model.HumanoidRootPart
	root.Anchored = true
	model:SetAttribute("Rig", "avatar")
	model:SetAttribute("FaceMoves", model.Head:FindFirstChildOfClass("FaceControls") ~= nil)
	model:SetAttribute("Scale", spec.size or 1)
	if spec.vest then
		vest(model, type(spec.vest) == "table" and spec.vest or {})
	end
	if spec.headset then
		headset(model, type(spec.headset) == "table" and spec.headset or {})
	end
	if spec.silhouette then
		silhouette(model, Util.color(spec.silhouette))
		model:SetAttribute("Silhouette", true)
	end
	if spec.eyes then
		eyes(model, spec.eyes)
	end
	model.Parent = parent
	root.CFrame = PARK
	return model
end

local function rigOf(model)
	local rig = rigs[model]
	if not rig then
		local humanoid = model:FindFirstChildOfClass("Humanoid")
		local animator = humanoid:FindFirstChildOfClass("Animator")
		if not animator then
			animator = Instance.new("Animator")
			animator.Parent = humanoid
		end
		rig = { animator = animator, tracks = {}, playing = {} }
		rigs[model] = rig
	end
	return rig
end

local function trackFor(model, content)
	local rig = rigOf(model)
	local track = rig.tracks[content]
	if track then
		return track
	end
	local animation = Instance.new("Animation")
	animation.AnimationId = content
	track = rig.animator:LoadAnimation(animation)
	local waited = 0
	while track.Length == 0 and waited < 10 do
		task.wait(0.05)
		waited += 0.05
	end
	if track.Length == 0 then
		error("animation did not load: " .. content)
	end
	rig.tracks[content] = track
	return track
end

local jointCFrame = Pose.jointCFrame

-- options.full: every joint is driven, so nothing of an animation underneath shows through.
-- options.linear: keys are close together and are joined straight, not eased one by one.
-- options.model: the avatar the clip is made for. The animator stretches a clip's movements by the model's
-- scale, so distances measured on a resized body (how far the hips drop to sit) are written divided by it.
function M.clip(keys, options)
	options = options or {}
	local scale = options.model and options.model:GetScale() or 1
	scale = math.floor(scale * 1000 + 0.5) / 1000
	local signature = HttpService:JSONEncode({ keys, options.full or false, options.linear or false, scale, options.controls or false })
	if clips[signature] then
		return clips[signature]
	end
	local joints, controls = {}, {}
	if options.full then
		for _, name in ipairs(ORDER) do
			joints[name] = true
		end
	end
	for _, key in ipairs(keys) do
		for name in pairs(key.joints or {}) do
			if not PARENT[name] then
				error("unknown avatar joint '" .. tostring(name) .. "'")
			end
			joints[name] = true
		end
		for name in pairs(key.face or {}) do
			controls[name] = true
		end
	end
	for _, name in ipairs(options.controls or {}) do
		controls[name] = true
	end
	local sequence = Instance.new("KeyframeSequence")
	sequence.Loop = false
	sequence.Priority = Enum.AnimationPriority.Action4
	for _, key in ipairs(keys) do
		local keyframe = Instance.new("Keyframe")
		keyframe.Time = key.t
		local poses = {}
		local rootPose = Instance.new("Pose")
		rootPose.Name = "HumanoidRootPart"
		rootPose.Weight = 0
		keyframe:AddPose(rootPose)
		poses.HumanoidRootPart = rootPose
		for _, name in ipairs(ORDER) do
			local pose = Instance.new("Pose")
			pose.Name = name
			if joints[name] then
				pose.Weight = 1
				local turn = jointCFrame((key.joints or {})[name] or {})
				pose.CFrame = CFrame.new(turn.Position / scale) * turn.Rotation
				pose.EasingStyle = options.linear and Enum.PoseEasingStyle.Linear or Enum.PoseEasingStyle.Cubic
				pose.EasingDirection = Enum.PoseEasingDirection.InOut
			else
				pose.Weight = 0
			end
			poses[PARENT[name]]:AddSubPose(pose)
			poses[name] = pose
		end
		if next(controls) then
			local folder = Instance.new("Folder")
			folder.Name = "FaceControls"
			folder.Parent = poses.Head
			for control in pairs(controls) do
				local value = Instance.new("NumberPose")
				value.Name = control
				value.Value = (key.face or {})[control] or 0
				value.Parent = folder
			end
		end
		sequence:AddKeyframe(keyframe)
	end
	local content = ClipProvider:RegisterAnimationClip(sequence)
	clips[signature] = content
	return content
end

function M.stage(model, plan)
	local rig = rigOf(model)
	for _, track in ipairs(rig.animator:GetPlayingAnimationTracks()) do
		track:Stop(0)
	end
	rig.playing = {}
	for _, layer in ipairs(plan) do
		local track = trackFor(model, layer.content)
		track.Priority = layer.priority or Enum.AnimationPriority.Core
		track:Play(0, layer.weight or 1, 0)
		table.insert(rig.playing, { track = track, layer = layer })
	end
end

function M.apply(model, rootCF, t, u)
	local rig = rigOf(model)
	model.HumanoidRootPart.CFrame = rootCF * CFrame.new(0, Pose.restLift(model), 0)
	for _, item in ipairs(rig.playing) do
		local layer, track = item.layer, item.track
		if layer.normalized then
			track.TimePosition = Util.clamp(u, 0, 0.999) * track.Length
		elseif layer.hold then
			track.TimePosition = Util.clamp(layer.hold, 0, 0.999) * track.Length
		else
			local time = (layer.phase or 0) * track.Length + t * (layer.speed or 1)
			if layer.loop == false then
				track.TimePosition = math.min(time, track.Length * 0.999)
			else
				track.TimePosition = time % track.Length
			end
		end
	end
	rig.animator:StepAnimations(0)
	return model.Head.Position
end

function M.park(model)
	local rig = rigOf(model)
	for _, track in ipairs(rig.animator:GetPlayingAnimationTracks()) do
		track:Stop(0)
	end
	rig.playing = {}
	model.HumanoidRootPart.CFrame = PARK
	rig.animator:StepAnimations(0)
end

function M.hand(model, side)
	local hand = model[side == "L" and "LeftHand" or "RightHand"]
	local grip = hand:FindFirstChild(side == "L" and "LeftGripAttachment" or "RightGripAttachment")
	return grip and grip.WorldCFrame or hand.CFrame
end

return M
