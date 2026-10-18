class Colors:
    # 深色板（WCAG AA 核算，线性化相对亮度 L）：
    #   TEXT_PRIMARY   #e0e0e8  L≈0.746
    #   TEXT_SECONDARY #a8a8c0  L≈0.401
    #   TEXT_DISABLED  #5a5a72  L≈0.107（仅限 disabled 控件，AA 豁免）
    #   BG_BASE        #1e1e2e  L≈0.013
    #   BG_ELEVATED    #2a2a3e  L≈0.025
    # 对比度：primary on elevated ≈ 10.6:1；secondary on elevated ≈ 6.0:1；
    #         primary on base ≈ 5.1:1；secondary on base ≈ 7.2:1 —— 均 ≥ 4.5:1。
    # 层级由字号/字重承担（14 正文 vs 12 辅助），颜色保持高对比、拒绝暗灰。
    BG_DEEPEST = "#16161e"
    BG_BASE = "#1e1e2e"
    BG_ELEVATED = "#2a2a3e"
    BG_HOVER = "#33334a"
    BG_ACTIVE = "#3e3e5c"
    BORDER = "#3a3a4e"
    BORDER_LIGHT = "#4a4a62"
    TEXT_PRIMARY = "#e0e0e8"
    TEXT_SECONDARY = "#a8a8c0"
    TEXT_DISABLED = "#5a5a72"
    ACCENT = "#4a9eff"
    ACCENT_HOVER = "#5eaaff"
    ACCENT_PRESSED = "#3a8eef"
    SUCCESS = "#4ade80"
    WARNING = "#f59e0b"
    WARNING_LIGHT = "#ffcc80"
    DANGER = "#ef4444"
    INFO = "#06b6d4"
    TEAM_ALLY = "#4a9eff"
    TEAM_ENEMY = "#ef4444"
