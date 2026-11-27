return function(b)
	b.info.lighting = "interior_night"
	b:floor(-9, 9, -7, 6, "#4b5563", { material = Enum.Material.Fabric })
	b:ceiling(-9, 9, -7, 6, 9, "#2f3542")
	b:wallX(-9, 9, -7.2, 9, "#39424e")
	b:wallZ(-7, 6, -9.2, 9, "#39424e")
	b:wallZ(-7, 6, 9.2, 9, "#39424e")
	b:box({ 9, 4.6, 0.2 }, { -2.5, 5.4, -7.05 }, "#dfe6ee")
	b:box({ 8.6, 4.2, 0.22 }, { -2.5, 5.4, -7.03 }, "#0d1b3a")
	for index = 0, 17 do
		local x = -6.3 + (index % 6) * 1.5
		local y = 3.9 + math.floor(index / 6) * 1.1
		if index % 4 ~= 1 then
			b:box({ 0.5, 0.5, 0.05 }, { x, y, -6.9 }, "#ffd966", { material = Enum.Material.Neon, shadow = false })
		end
	end
	b:box({ 1.6, 1.6, 0.05 }, { 0.6, 6.6, -6.9 }, "#f5f3ce", { material = Enum.Material.Neon, shape = Enum.PartType.Ball, shadow = false })
	b:sign("11:47 PM", { 3, 1, 0.2 }, { 5.6, 6.8, -7.0 }, "#111111", "#ff5e57", { font = Enum.Font.Code, unlit = true })
	b:box({ 2.6, 0.25, 5.4 }, { 2.4, 2.9, -3 }, "#8a6a4a")
	b:box({ 2.2, 2.8, 0.25 }, { 2.4, 1.4, -5.5 }, "#6f5335")
	b:box({ 2.2, 2.8, 0.25 }, { 2.4, 1.4, -0.5 }, "#6f5335")
	b:box({ 0.25, 2.0, 3.0 }, { 3.2, 4.3, -3 }, "#111111")
	b:box({ 0.06, 1.7, 2.7 }, { 3.05, 4.3, -3 }, "#bfe6ff", { material = Enum.Material.Neon, shadow = false })
	b:box({ 0.5, 0.5, 0.8 }, { 3.2, 3.2, -3 }, "#222222")
	b:box({ 0.9, 0.1, 2.2 }, { 1.8, 3.07, -3 }, "#222222")
	b:box({ 1.2, 0.7, 0.9 }, { 2.6, 3.4, -0.9 }, "#f5f1e6")
	b:box({ 1.2, 0.5, 0.9 }, { 2.5, 3.3, -5.0 }, "#f5f1e6")
	b:box({ 0.5, 0.55, 0.5 }, { 1.7, 3.3, -1.5 }, "#d9534f")
	b:box({ 1.6, 0.3, 1.6 }, { 0, 1.35, -3 }, "#1f2933")
	b:box({ 0.3, 2.6, 1.6 }, { -0.8, 2.7, -3 }, "#1f2933")
	b:light({ 2.2, 4.4, -3 }, "#bfe6ff", 2.2, 12)
	b:light({ -4, 7, 0 }, "#6d7fb3", 0.6, 20)
	b:anchor("office_chair", 0, 0, -3, -90)
	b:anchor("office_door", -6, 0, 2, -90)
	b:camera("office_front", { 7.6, 4.8, 3.2 }, { 0.6, 3.9, -3.1 })
	b:camera("office_side", { -0.5, 4.6, 5.8 }, { 1.2, 3.9, -3 })
end
