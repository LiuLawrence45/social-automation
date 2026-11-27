local RunService = game:GetService("RunService")
local CoreGui = game:GetService("CoreGui")

local M = {}

M.CELL = 6
M.BITS = 16
M.MARGIN = 2

function M.settle(frames)
	for _ = 1, frames do
		RunService.Heartbeat:Wait()
	end
end

local function gui()
	local found = CoreGui:FindFirstChild("DirectorMarker")
	if not found then
		found = Instance.new("ScreenGui")
		found.Name = "DirectorMarker"
		found.IgnoreGuiInset = true
		found.DisplayOrder = 1000000
		found.ResetOnSpawn = false
		found.ZIndexBehavior = Enum.ZIndexBehavior.Sibling
		found.Parent = CoreGui
	end
	return found
end

local function cell(parent, name, x, y, width, height, color)
	local frame = parent:FindFirstChild(name)
	if not frame then
		frame = Instance.new("Frame")
		frame.Name = name
		frame.BorderSizePixel = 0
		frame.Parent = parent
	end
	frame.Position = UDim2.fromOffset(x, y)
	frame.Size = UDim2.fromOffset(width, height)
	frame.BackgroundColor3 = color
	return frame
end

-- A row of cells in the top-left corner of the viewport: white, black, 16 bits of the frame number
-- (most significant first, white = 1), black, white. The capture side reads it back from the picture,
-- so a captured frame is known to show the frame that was asked for.
function M.marker(frameIndex)
	local screen = gui()
	local strip = screen:FindFirstChild("Strip")
	if frameIndex == nil then
		if strip then
			strip:Destroy()
		end
		return
	end
	local count = M.BITS + 4
	strip = cell(screen, "Strip", 0, 0, count * M.CELL + 2 * M.MARGIN, M.CELL + 2 * M.MARGIN, Color3.new(0, 0, 0))
	strip.ZIndex = 20
	local white, black = Color3.new(1, 1, 1), Color3.new(0, 0, 0)
	local pattern = { white, black }
	for bit = M.BITS - 1, 0, -1 do
		table.insert(pattern, bit32.extract(frameIndex, bit) == 1 and white or black)
	end
	table.insert(pattern, black)
	table.insert(pattern, white)
	for index, color in ipairs(pattern) do
		cell(strip, "c" .. index, M.MARGIN + (index - 1) * M.CELL, M.MARGIN, M.CELL, M.CELL, color)
	end
end

-- Covers the whole viewport with one flat colour so the capture side can find the viewport
-- rectangle inside the window picture.
function M.calibrate(on)
	local screen = gui()
	local cover = screen:FindFirstChild("Cover")
	if not on then
		if cover then
			cover:Destroy()
		end
		return
	end
	if not cover then
		cover = Instance.new("Frame")
		cover.Name = "Cover"
		cover.BorderSizePixel = 0
		cover.Size = UDim2.fromScale(1, 1)
		cover.BackgroundColor3 = Color3.new(1, 0, 1)
		cover.ZIndex = 10
		cover.Parent = screen
	end
end

function M.clear()
	local found = CoreGui:FindFirstChild("DirectorMarker")
	if found then
		found:Destroy()
	end
end

-- Probe of StudioCaptureService, kept so a later Studio version can be re-tested with one command.
-- Studio 0.748: CanCaptureScreenshot() is false, RequestScreenshotPermissionAsync() raises
-- "Feature not supported yet." and CaptureScreenshot() returns nil.
function M.serviceProbe()
	local service = game:GetService("StudioCaptureService")
	local out = { can = service:CanCaptureScreenshot() }
	local ok, result = pcall(function()
		return service:RequestScreenshotPermissionAsync()
	end)
	out.permission = ok and result or tostring(result)
	local viewport = workspace.CurrentCamera.ViewportSize
	local okShot, shot = pcall(function()
		return service:CaptureScreenshot({ Position = Vector2.new(0, 0), CaptureSize = viewport })
	end)
	out.capture = okShot and (shot ~= nil and "returned a capture" or "returned nil") or tostring(shot)
	return out
end

return M
