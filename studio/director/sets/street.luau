return function(b)
	b.info.lighting = "day"
	b:floor(-60, 60, -40, 50, "#69c04a", { material = Enum.Material.Grass })
	b:box({ 120, 0.3, 8 }, { 0, 0.15, 5 }, "#d2d2d2", { material = Enum.Material.Concrete })
	b:box({ 120, 0.2, 11 }, { 0, 0.1, 14.5 }, "#47474d")
	for x = -54, 54, 9 do
		b:box({ 4, 0.02, 0.4 }, { x, 0.21, 14.5 }, "#f4d35e", { shadow = false })
	end
	b:box({ 26, 38, 12 }, { 0, 19, -6 }, "#b5533c", { material = Enum.Material.Brick })
	b:box({ 26.6, 1, 12.6 }, { 0, 38.5, -6 }, "#7a3526")
	for floorIndex = 0, 3 do
		for col = -2, 2 do
			if not (floorIndex == 0 and col == 0) then
				local x, y = col * 5, 5.2 + floorIndex * 8.4
				b:box({ 3.2, 4.2, 0.3 }, { x, y, 0.05 }, "#fff7e6")
				b:box({ 2.8, 3.8, 0.32 }, { x, y, 0.06 }, (floorIndex + col) % 3 == 0 and "#ffe9a8" or "#9fd8ff")
				b:box({ 0.16, 3.8, 0.36 }, { x, y, 0.07 }, "#fff7e6")
				b:box({ 3.6, 0.3, 0.7 }, { x, y - 2.2, 0.3 }, "#fff7e6")
			end
		end
	end
	b:box({ 3.4, 6.4, 0.4 }, { 0, 3.5, 0.1 }, "#2f4858")
	b:box({ 0.3, 0.3, 0.3 }, { 1.1, 3.4, 0.4 }, "#e3c15a", { shape = Enum.PartType.Ball })
	b:box({ 7, 0.5, 3.4 }, { 0, 7.3, 1.6 }, "#2e8b57")
	b:sign("MAPLE COURT", { 6.4, 1.1, 0.2 }, { 0, 8.3, 0.2 }, "#f7f3e3", "#2b2b2b")
	b:box({ 5, 0.3, 2 }, { 0, 0.3, 1 }, "#bdbdbd")
	b:box({ 18, 26, 12 }, { -24, 13, -6 }, "#e0b06a", { material = Enum.Material.Brick })
	b:box({ 18, 30, 12 }, { 36, 15, -8 }, "#8fa7bf", { material = Enum.Material.Brick })
	for index = 0, 5 do
		b:box({ 3, 3.6, 0.3 }, { -29 + (index % 3) * 5, 6 + math.floor(index / 3) * 9, 0.05 }, "#9fd8ff")
		b:box({ 3, 3.6, 0.3 }, { 31 + (index % 3) * 5, 8 + math.floor(index / 3) * 9, -1.95 }, "#9fd8ff")
	end
	for _, x in ipairs({ -11, 9 }) do
		b:box({ 1.2, 7, 1.2 }, { x, 3.5, 2.6 }, "#7a4a2a")
		b:box({ 6, 6, 6 }, { x, 9, 2.6 }, "#3f9b3f", { shape = Enum.PartType.Ball })
		b:box({ 4.4, 4.4, 4.4 }, { x + 1.4, 11.6, 2.6 }, "#52b352", { shape = Enum.PartType.Ball })
	end
	b:box({ 0.4, 10, 0.4 }, { -4.5, 5, 8.4 }, "#2b2b2b")
	b:box({ 1.2, 0.6, 1.2 }, { -4.5, 10.2, 8.4 }, "#ffe9a8", { material = Enum.Material.Neon })
	b:box({ 5.4, 2.4, 2.2 }, { 18, 1.5, 2.4 }, "#deb887", { material = Enum.Material.Wood })
	for index = 0, 5 do
		b:box({ 1, 0.3, 4.2 }, { 15.5 + index, 7.4, 3 }, index % 2 == 0 and "#e2574c" or "#ffffff", { rot = { 14, 0, 0 } })
	end
	b:box({ 0.25, 7, 0.25 }, { 15.2, 3.8, 4.8 }, "#8a5a2b")
	b:box({ 0.25, 7, 0.25 }, { 20.8, 3.8, 4.8 }, "#8a5a2b")
	b:box({ 0.25, 8, 0.25 }, { 15.2, 4.3, 1.2 }, "#8a5a2b")
	b:box({ 0.25, 8, 0.25 }, { 20.8, 4.3, 1.2 }, "#8a5a2b")
	b:sign("FLOWERS", { 4.6, 1.1, 0.2 }, { 18, 6.3, 5.0 }, "#fff7e6", "#d6336c")
	local blooms = { "#ff6f91", "#ffd166", "#f72585", "#ffffff", "#ff9770", "#b388eb" }
	for index = 0, 4 do
		local x = 16 + index
		b:box({ 0.7, 0.8, 0.7 }, { x, 3.1, 2.6 }, "#6c8ebf")
		for k = 0, 2 do
			b:box({ 0.5, 0.5, 0.5 }, { x - 0.2 + k * 0.2, 3.8 + (k % 2) * 0.25, 2.5 + k * 0.1 }, blooms[(index + k) % #blooms + 1], { shape = Enum.PartType.Ball })
		end
	end
	for index = 0, 2 do
		local x = 16.4 + index * 1.6
		b:box({ 0.9, 1, 0.9 }, { x, 0.8, 4.4 }, "#8fbf6c")
		for k = 0, 2 do
			b:box({ 0.55, 0.55, 0.55 }, { x - 0.25 + k * 0.25, 1.6 + (k % 2) * 0.3, 4.4 }, blooms[(index * 2 + k) % #blooms + 1], { shape = Enum.PartType.Ball })
		end
	end
	b:anchor("entrance", 0, 0.3, 3.2, 180)
	b:anchor("sidewalk_left", -8, 0.3, 5.5, -90)
	b:anchor("sidewalk_right", 8, 0.3, 5.5, 90)
	b:anchor("sidewalk_mid", 0, 0.3, 6, 180)
	b:anchor("curb", 2, 0.3, 8.2, 180)
	b:anchor("curb_left", -1.5, 0.3, 8.2, 180)
	b:anchor("road_start", -6, 0.2, 14.5, -90)
	b:anchor("road_end", 46, 0.2, 14.5, -90)
	b:anchor("stand_customer", 18, 0.3, 6.6, 0)
	b:anchor("vendor", 18, 0.3, 0.6, 180)
	b:anchor("stand_table", 18, 2.72, 3.0, 180)
	b:camera("facade", { 0, 8, 38 }, { 0, 17, 0 })
	b:camera("facade_low", { 0, 2.4, 20 }, { 0, 20, 0 })
	b:camera("stand", { 18, 5, 15 }, { 18, 3.6, 2 })
end
