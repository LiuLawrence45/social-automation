return function(b)
	b.info.lighting = "interior"
	b:floor(-14, 14, -8, 7, "#ece6d8")
	for i = -7, 6 do
		for j = -4, 3 do
			if (i + j) % 2 == 0 then
				b:box({ 2, 0.03, 2 }, { i * 2 + 1, 0.015, j * 2 + 1 - 0.5 }, "#3f4b57", { shadow = false })
			end
		end
	end
	b:ceiling(-14, 14, -8, 7, 9, "#f6f2e8")
	b:wallX(-14, 14, -8.2, 9, "#d5e4df")
	b:wallX(-14, 14, -7.96, 3, "#9bb7ae", { thick = 0.08 })
	b:wallZ(-8, 7, -14.2, 9, "#d5e4df")
	b:wallZ(-8, 7, 14.2, 9, "#d5e4df")
	for x = -8, 8, 8 do
		b:box({ 2.6, 0.1, 1.2 }, { x, 8.94, -2 }, "#fff3cf", { material = Enum.Material.Neon, shadow = false })
		b:light({ x, 7.6, -2 }, "#fff1d6", 0.8, 22)
	end
	for row = 0, 2 do
		for col = 0, 3 do
			b:box({ 1.0, 0.8, 0.3 }, { -12.4 + col * 1.15, 4.2 + row * 0.95, -7.9 }, "#c9a23f")
			b:box({ 0.3, 0.12, 0.05 }, { -12.4 + col * 1.15, 4.1 + row * 0.95, -7.72 }, "#6b5416")
		end
	end
	b:sign("MAIL", { 2.4, 0.7, 0.1 }, { -10.7, 7.2, -7.9 }, "#3f4b57", "#ffffff")
	b:box({ 6.4, 3.2, 1.8 }, { -3, 1.6, -3.6 }, "#7a4a2a")
	b:box({ 6.8, 0.25, 2.2 }, { -3, 3.3, -3.6 }, "#5c3317")
	b:sign("MANAGER", { 3.4, 0.9, 0.1 }, { -3, 2.1, -2.66 }, "#f4d35e", "#3a2a10")
	b:box({ 0.5, 0.3, 0.5 }, { -1.2, 3.55, -3.2 }, "#e3c15a", { shape = Enum.PartType.Ball })
	b:box({ 1.4, 1.1, 0.2 }, { -4.6, 4.0, -3.9 }, "#222222", { rot = { 0, 20, 0 } })
	b:box({ 0.4, 0.4, 0.4 }, { -4.6, 3.5, -3.9 }, "#222222")
	b:box({ 3.4, 2.4, 0.15 }, { -3, 6.2, -7.95 }, "#6b4a2b")
	for index = 0, 7 do
		b:box({ 0.12, 0.5, 0.08 }, { -4.2 + index * 0.35, 6.1, -7.84 }, "#e3c15a")
	end
	b:box({ 5, 7.8, 0.3 }, { 7, 3.9, -8.0 }, "#8d99a6")
	b:box({ 2.0, 6.9, 0.2 }, { 5.98, 3.45, -7.84 }, "#b8c4cf", { material = Enum.Material.Metal })
	b:box({ 2.0, 6.9, 0.2 }, { 8.02, 3.45, -7.84 }, "#b8c4cf", { material = Enum.Material.Metal })
	b:box({ 0.06, 6.9, 0.22 }, { 7, 3.45, -7.83 }, "#5c6670")
	b:box({ 1.6, 0.6, 0.12 }, { 7, 8.2, -7.82 }, "#111111")
	b:box({ 0.5, 1.1, 0.12 }, { 10.1, 4, -7.9 }, "#333333")
	b:box({ 0.22, 0.22, 0.08 }, { 10.1, 4.2, -7.82 }, "#ff5e57", { material = Enum.Material.Neon, shape = Enum.PartType.Ball })
	b:box({ 1.3, 1.3, 1.3 }, { 12.4, 0.65, -6.6 }, "#b5653a")
	b:box({ 2, 2, 2 }, { 12.4, 2.4, -6.6 }, "#4c9a4a", { shape = Enum.PartType.Ball })
	b:box({ 1.4, 1.4, 1.4 }, { 12.6, 3.6, -6.6 }, "#5fb35c", { shape = Enum.PartType.Ball })
	b:anchor("manager_spot", -3, 0, -5.6, 180)
	b:anchor("desk_front", -3, 0, -1.0, 0)
	b:anchor("desk_front_left", -5.4, 0, -0.8, 0)
	b:anchor("desk_front_right", -0.6, 0, -0.8, 0)
	b:anchor("elevator_front", 7, 0, -5.2, 0)
	b:anchor("elevator_left", 5.2, 0, -5.0, 0)
	b:anchor("elevator_right", 9.0, 0, -4.8, 0)
	b:anchor("elevator_sign", 7, 4.6, -7.7, 180)
	b:anchor("lobby_mid", 0, 0, 0, 180)
	b:anchor("lobby_right", 11, 0, 0, 90)
	b:camera("desk_wide", { -3, 4.8, 6.6 }, { -3, 3.6, -5 })
	b:camera("elevator_wide", { 7, 4.8, 6.6 }, { 7, 3.8, -7 })
end
