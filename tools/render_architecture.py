"""Regenerate the editable SVG architecture diagram; --png additionally needs Pillow."""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path


def render(project: Path, png: bool = False) -> None:
    width, height = 1400, 1070
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
           '<title id="title">星河虚拟银行 Agent 系统架构</title>',
           '<desc id="desc">同机浏览器和命令行经过规则解析，调用任务状态机、工具注册表、内存模拟银行和本地审计。没有真实银行、语言模型或后台调度。</desc>',
           '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#8c86a6"/></marker></defs>',
           '<rect width="1400" height="1070" fill="#f7f6fc"/>',
           '<g font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif">']
    canvas = draw = font_path = None
    if png:
        from PIL import Image, ImageDraw, ImageFont
        canvas = Image.new("RGB", (width, height), "#f7f6fc")
        draw = ImageDraw.Draw(canvas)
        font_path = next((path for path in (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ) if path.is_file()), None)
        if font_path is None:
            raise RuntimeError("A Chinese font is required to render PNG; SVG can be generated without --png.")

    def text(x, y, value, size=18, color="#646078", bold=False):
        svg.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{700 if bold else 400}">{escape(value)}</text>')
        if draw:
            font = ImageFont.truetype(str(font_path), size=size)
            draw.text((x, y - size - 2), value, font=font, fill=color)

    def box(x, y, w, h, title, lines, fill="#ffffff", border="#ddd8ed"):
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="18" fill="{fill}" stroke="{border}" stroke-width="1.5"/>')
        if draw:
            draw.rounded_rectangle((x, y, x+w, y+h), radius=18, fill=fill, outline=border, width=2)
        text(x+22, y+35, title, 21, "#30283f", True)
        for n, line in enumerate(lines):
            text(x+22, y+65+n*25, line, 16)

    def arrow(points):
        joined = " ".join(f"{x},{y}" for x,y in points)
        svg.append(f'<polyline points="{joined}" fill="none" stroke="#8c86a6" stroke-width="2" marker-end="url(#arrow)"/>')
        if draw:
            draw.line(points, fill="#8c86a6", width=2)
            x,y = points[-1]
            prev_x,prev_y = points[-2]
            if y > prev_y:
                tip = [(x,y),(x-5,y-10),(x+5,y-10)]
            else:
                tip = [(x,y),(x-10,y-5),(x-10,y+5)]
            draw.polygon(tip, fill="#8c86a6")

    text(48, 52, "星河虚拟银行 Agent", 32, "#292039", True)
    text(48, 84, "系统架构总览  /  已实现的本机教学原型", 18)
    text(1120, 64, "文档基准 2026-10-29", 16)
    box(48, 120, 620, 90, "浏览器工作台", ["聊天 · 表单 · 首次收款人核对 · 业务按钮"])
    box(732, 120, 620, 90, "命令行演示", ["中文指令 · 字段补齐 · 核心默认确认"])
    box(48, 280, 620, 110, "本机 HTTP 与浏览器会话", ["LocalBankServer / BrowserSession · 仅 127.0.0.1", "Host / Origin / CSRF · Cookie · revision · 请求去重"])
    box(732, 280, 620, 110, "规则对话与顺序工作流", ["DemoConversation / BrowserDemoConversation", "结构化 OperationRequest · BankingWorkflow"])
    arrow([(358,210),(358,280)])
    arrow([(1042,210),(1042,280)])
    arrow([(668,335),(732,335)])
    arrow([(1042,390),(1042,455)])
    box(48, 455, 1304, 205, "BankingAgent / BrowserDemoAgent  ·  编排与策略核心", [], "#eee8fc", "#c5b6e8")
    core_nodes = [
        (68,"任务与字段",["用户 / 会话归属绑定","缺配置、缺工具时阻断"]),
        (394,"认证与风险",["STANDARD / ENHANCED","R0—R4 风险只升不降"]),
        (720,"预览与授权",["摘要绑定、有效期、复验","显式确认 / 小额 / 范围"]),
        (1046,"幂等执行",["原幂等键查询结果","失败或未知不自动重提"]),
    ]
    for x,title,lines in core_nodes:
        box(x,515,286,123,title,lines)
    for x in (354,680,1006):
        arrow([(x,575),(x+40,575)])
    box(48, 730, 850, 125, "ToolRegistry → SimulatedBank", ["同一模拟实例实现认证、风控、解析、预览、执行、查询和审核", "SyntheticStockMarket：固定虚构报价、Decimal 费用、独立持仓"])
    box(950, 730, 402, 125, "AuditLog / privacy", ["执行前审计 · 追加 JSONL", "标识 HMAC · SHA-256 日志链"])
    arrow([(472,660),(472,730)])
    arrow([(1150,660),(1150,730)])
    box(48, 920, 850, 90, "每轮会话的内存数据", ["账户、联系人、任务、持仓、计划、幂等结果；重置或重启恢复样例"])
    box(950, 920, 402, 90, "本地磁盘", ["data/*.jsonl；不用于恢复业务状态"])
    arrow([(472,855),(472,920)])
    arrow([(1150,855),(1150,920)])
    text(48, 1048, "全部数据为虚构样例 · 没有真实银行 / LLM / 数据库 / 后台调度 · 此图不是独立微服务部署图", 17)
    svg.extend(["</g>","</svg>"])
    destination = project / "docs"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "架构图.svg").write_text("\n".join(svg)+"\n", encoding="utf-8")
    if canvas:
        canvas.save(destination / "架构图.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true", help="also render PNG using Pillow and a Chinese font")
    arguments = parser.parse_args()
    render(Path(__file__).resolve().parents[1], arguments.png)
