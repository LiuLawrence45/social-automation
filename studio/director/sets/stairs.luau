return function(b)
	b.info.lighting = "interior"
	b:floor(-18, 18, -3, 7, "#8f979e")
	b:wallX(-18, 18, -2.9, 24, "#dfe7ee")
	b:wallX(-18, 18, -2.66, 24, "#c7d2dc", { thick = 0.06 })
	for index = 0, 13 do
		b:box({ 1.2, 0.8 * (index + 1), 5 }, { -8 + 1.2 * index + 0.6, 0.4 * (index + 1), 0 }, index % 2 == 0 and "#b4bcc4" or "#a7b0b9")
		b:box({ 1.22, 0.08, 5.02 }, { -8 + 1.2 * index + 0.6, 0.8 * (index + 1), 0 }, "#6c7780", { shadow = false })
	end
	b:box({ 9, 11.2, 5 }, { 13.3, 5.6, 0 }, "#b4bcc4")
	b:box({ 21, 0.3, 0.3 }, { 0.4, 8.6, 2.4 }, "#c0392b", { rot = { 0, 0, 33.7 } })
	for index = 0, 6 do
		local x = -7.4 + index * 2.6
		b:box({ 0.2, 3, 0.2 }, { x, 0.8 * ((x + 8) / 1.2) + 1.6, 2.4 }, "#c0392b")
	end
	b:box({ 9, 0.3, 0.3 }, { 13.3, 14.2, 2.4 }, "#c0392b")
	local signs = { { "1", -11.5, 5.5 }, { "2", -3.5, 9.5 }, { "3", 3.5, 14 }, { "4", 13.3, 17.5 } }
	for _, sign in ipairs(signs) do
		b:sign(sign[1], { 2.4, 2.4, 0.2 }, { sign[2], sign[3], -2.5 }, "#33415c", "#ffffff")
	end
	b:box({ 3, 6.6, 0.3 }, { 13.3, 14.5, -2.5 }, "#7a4a2a")
	b:light({ 0, 14, 4 }, "#ffffff", 0.6, 40)
	b:anchor("stairs_bottom", -9.2, 0, 0, -90)
	b:anchor("stairs_low", -5, 2.0, 0, -90)
	b:anchor("stairs_mid", 0.4, 5.6, 0, -90)
	b:anchor("stairs_high", 6, 9.33, 0, -90)
	b:anchor("stairs_top", 9.6, 11.2, 0, -90)
	b:anchor("landing", 13.3, 11.2, 0.4, 180)
	b:anchor("landing_side", 15.6, 11.2, 0.8, 180)
	b:anchor("bottom_wait", -12.5, 0, 1, 180)
	b:camera("stairs_wide", { 2, 8.5, 26 }, { 2, 7.5, 0 })
	b:camera("landing_view", { 13.3, 15.6, 9 }, { 13.3, 14.4, 0 })
end
