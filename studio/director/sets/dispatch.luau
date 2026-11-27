return function(b)
	b.info.lighting = "dispatch"
	local wall = "#2c3442"
	b:floor(-7, 7, -5, 10, "#20242c", { material = Enum.Material.Fabric })
	b:ceiling(-7, 7, -5, 4, 9, "#262b35")
	b:wallX(-7, 7, -5.2, 9, wall)
	b:wallZ(-5, 4, -7.2, 9, wall)
	b:wallZ(-5, 4, 7.2, 9, wall)
	b:sign("911 DISPATCH", { 6, 1.0, 0.15 }, { 0, 7.4, -4.95 }, "#141a24", "#7fd6ff", { font = Enum.Font.GothamBold, ppi = 60, unlit = true })
	b:box({ 8, 0.3, 3.2 }, { 0, 3.1, -1.1 }, "#3a4252", { material = Enum.Material.Metal })
	b:box({ 8, 2.95, 0.3 }, { 0, 1.48, -1.6 }, "#2f3644")
	b:wallX(-7, 7, 4.2, 9, wall)
	b:sign(b:word("dispatch_duty", "ON DUTY  RILEY"), { 6.4, 1.0, 0.15 }, { -1.2, 7.3, 3.95 }, "#141a24", "#f4f1e6", { font = Enum.Font.GothamBold, ppi = 60, unlit = true, face = Enum.NormalId.Front })
	for index, x in ipairs({ -3.2, -1.0 }) do
		local turn = (2 - index) * 22
		b:box({ 2.1, 1.4, 0.12 }, { x, 4.5, -2.5 + math.abs(x) * 0.18 }, "#0a0d12", { rot = { 0, turn, 0 } })
		b:box({ 1.9, 1.2, 0.04 }, { x, 4.5, -2.42 + math.abs(x) * 0.18 }, index == 2 and "#3fa7ff" or "#35d0b0", { rot = { 0, turn, 0 }, material = Enum.Material.Neon, shadow = false })
		b:box({ 0.2, 0.8, 0.2 }, { x, 3.5, -2.5 + math.abs(x) * 0.18 }, "#1a1d24")
	end
	b:light({ 0, 4.6, -3.4 }, "#5fc8ff", 3.0, 14, { name = "screens", shadows = true })
	b:light({ 0, 8.2, 2 }, "#3a4a70", 1.0, 20)
	b:box({ 1.6, 0.25, 1.6 }, { -1.4, 1.75, 1.6 }, "#1b1f27", { material = Enum.Material.Fabric, name = "Seat" })
	b:box({ 1.6, 2.3, 0.25 }, { -1.4, 3.0, 2.5 }, "#1b1f27", { material = Enum.Material.Fabric })
	b:box({ 0.3, 1.6, 0.3 }, { -1.4, 0.85, 1.6 }, "#0e1014", { material = Enum.Material.Metal })
	b:box({ 1.5, 0.12, 1.5 }, { -1.4, 0.06, 1.6 }, "#0e1014", { material = Enum.Material.Metal })
	b:box({ 1.6, 0.08, 0.5 }, { -1.4, 3.29, 0.1 }, "#12151b", { name = "Keyboard" })
	b:box({ 0.5, 0.14, 0.7 }, { 0.2, 3.32, -0.2 }, "#12151b")
	b:anchor("seat", -1.4, 1.875, 1.5, 0)
	b:anchor("keyboard", -1.4, 3.33, 0.1, 0)
	b:anchor("screens", -2.0, 4.5, -2.5, 0)
	b:anchor("plate", 2.4, 3.55, -0.4, 0)
	b:camera("desk_front", { 1.6, 4.8, 6.4 }, { 0.6, 4.3, -3.6 })
end
