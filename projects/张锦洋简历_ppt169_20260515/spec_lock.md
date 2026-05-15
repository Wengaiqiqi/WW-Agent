# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## colors
- bg: #FFFFFF
- secondary_bg: #F4F6F9
- primary: #1565C0
- accent: #0D47A1
- secondary_accent: #42A5F5
- text: #212121
- text_secondary: #616161
- text_tertiary: #9E9E9E
- border: #E0E0E0
- success: #2E7D32

## typography
- font_family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif
- code_family: Consolas, "Courier New", monospace
- body: 20
- cover_title: 64
- title: 36
- subtitle: 26
- annotation: 15
- footnote: 12

## icons
- library: tabler-filled
- inventory: user, phone-call, mail, map-pin, school, book, briefcase, robot, trophy, code-circle, cpu, settings, database, award, sparkles, circle-check, calendar, target, info-circle, heart

## page_rhythm
- P01: anchor
- P02: dense
- P03: breathing
- P04: dense
- P05: dense
- P06: dense
- P07: dense
- P08: dense
- P09: breathing
- P10: dense
- P11: dense
- P12: breathing
- P13: anchor

## page_charts
- P09: icon_grid
- P10: vertical_list
- P11: vertical_list

## forbidden
- Mixing icon libraries
- rgba()
- <style>, class, <foreignObject>, textPath, @font-face, <animate*>, <script>, <iframe>, <symbol>+<use>
- <g opacity> (set opacity on each child element individually)
- HTML named entities in text (&nbsp;, &mdash;, &copy;, &ndash;, &reg;, &hellip;, &bull;) — write as raw Unicode (—, ©, →, NBSP, etc.); XML reserved chars & < > " ' must be escaped as &amp; &lt; &gt; &quot; &apos;
