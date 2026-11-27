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

local function describe(spec)
	local description
	if spec.bundle then
		for _, item in ipairs(AssetService:GetBundleDetailsAsync(spec.bundle).Items) do
			if item.Type == "UserOutfit" then
				description = Players:GetHumanoidDescriptionFromOutfitId(item.Id)
				break
			end
		end
	end
	description = description or Instance.new("HumanoidDescription")
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
	model:SetAttribute("Scale", spec.size or 1)
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
	local signature = HttpService:JSONEncode({ keys, options.full or false, options.linear or false, scale })
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
