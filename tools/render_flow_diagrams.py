"""Render standalone sequence/state delivery diagrams from the architecture source.

SVG generation uses the standard library. PNG generation additionally uses Pillow
and an installed Chinese font. These are documentation maintenance dependencies.
"""

from __future__ import annotations

from html import escape
import math
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "分组交付素材" / "系统架构图"
BG = "#f7f6fc"
INK = "#30283f"
MUTED = "#686078"
PURPLE = "#7958c9"
TEAL = "#277d7a"
GRAY = "#91899f"


class Canvas:
    def __init__(self, width: int, height: int, title: str):
        from PIL import Image, ImageDraw, ImageFont

        self.ImageFont = ImageFont
        self.width, self.height = width, height
        self.font_path = next((path for path in (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ) if path.is_file()), None)
        if self.font_path is None:
            raise RuntimeError("A Chinese font is required for PNG generation.")
        self.image = Image.new("RGB", (width, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.fonts = {}
        self.svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title">',
            f'<title id="title">{escape(title)}</title>',
            f'<rect width="{width}" height="{height}" fill="{BG}"/>',
            '<g font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif">',
        ]

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = self.ImageFont.truetype(str(self.font_path), size)
        return self.fonts[size]

    def rect(self, x, y, width, height, fill="#ffffff", border="#ddd6ed", radius=17):
        self.svg.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" fill="{fill}" stroke="{border}" stroke-width="1.5"/>')
        self.draw.rounded_rectangle((x, y, x+width, y+height), radius=radius, fill=fill, outline=border, width=2)

    def text(self, x, y, text, size=18, color=MUTED, center=False, bold=False, backing=False):
        length = self.draw.textlength(text, font=self.font(size))
        draw_x = x - length / 2 if center else x
        if backing:
            self.svg.append(f'<rect x="{draw_x-5}" y="{y-size-3}" width="{length+10}" height="{size+10}" rx="4" fill="{BG}"/>')
            self.draw.rectangle((draw_x-5, y-size-3, draw_x+length+5, y+7), fill=BG)
        anchor = "middle" if center else "start"
        self.svg.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{700 if bold else 400}">{escape(text)}</text>')
        self.draw.text((draw_x, y), text, font=self.font(size), fill=color, anchor="ls")

    def line(self, points, color=GRAY, width=2, dashed=False, arrow=True):
        joined = " ".join(f"{x},{y}" for x,y in points)
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        self.svg.append(f'<polyline points="{joined}" fill="none" stroke="{color}" stroke-width="{width}"{dash}/>')
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            length = math.hypot(x2-x1, y2-y1)
            if dashed and length:
                distance = 0
                while distance < length:
                    end = min(distance+7, length)
                    self.draw.line((x1+(x2-x1)*distance/length, y1+(y2-y1)*distance/length,
                                    x1+(x2-x1)*end/length, y1+(y2-y1)*end/length), fill=color, width=width)
                    distance += 13
            else:
                self.draw.line(((x1,y1),(x2,y2)), fill=color, width=width)
        if arrow:
            x,y = points[-1]
            px,py = points[-2]
            angle = math.atan2(y-py,x-px)
            a = (x-12*math.cos(angle)+5*math.sin(angle),y-12*math.sin(angle)-5*math.cos(angle))
            b = (x-12*math.cos(angle)-5*math.sin(angle),y-12*math.sin(angle)+5*math.cos(angle))
            self.svg.append('<polygon points="'+' '.join(f'{qx},{qy}' for qx,qy in ((x,y),a,b))+f'" fill="{color}"/>')
            self.draw.polygon(((x,y),a,b), fill=color)

    def node(self, x, y, width, height, title, lines, fill="#ffffff", size=20):
        self.rect(x,y,width,height,fill)
        self.text(x+width/2,y+35,title,size,INK,True,True)
        for n,line in enumerate(lines):
            self.text(x+width/2,y+65+n*25,line,17,MUTED,True)

    def save(self, name):
        (OUT / (name+".svg")).write_text("\n".join(self.svg+["</g>","</svg>"])+"\n",encoding="utf-8")
        self.image.save(OUT / (name+".png"))


def sequence_diagram():
    c = Canvas(1620,1430,"首次收款人转账请求时序图")
    c.text(45,53,"请求时序图｜首次收款人 2888 元转账",31,INK,bold=True)
    c.text(45,88,"网页普通即时成功路径 · 全部为本机虚构数据 · 简化图；完整 Mermaid 见《请求时序图.md》",18)
    participants = [
        (140,"用户 / 浏览器","输入与弹窗"), (470,"HTTP / 会话","校验、解析与状态"),
        (800,"BrowserDemoAgent","认证、风险与编排"), (1130,"SimulatedBank","内存模拟工具"),
        (1460,"AuditLog","本地审计"),
    ]
    for x,title,sub in participants:
        c.rect(x-120,120,240,72)
        c.text(x,151,title,20,INK,True,True)
        c.text(x,177,sub,16,MUTED,True)
        c.line([(x,192),(x,1235)],"#c9c0dc",1,True,False)
    messages = [
        (0,1,"1. 给张三转账 2888 元"),
        (1,2,"2. 校验请求与字段；规则解析后 submit"),
        (2,3,"3. authenticate(ENHANCED)"),
        (2,4,"4. 写入认证结果 auth_checked"),
        (2,1,"5. 首次身份待核对；原任务暂停"),
        (1,0,"6. 显示身份弹窗；此时尚未扣款"),
        (0,1,"7. 确认虚构姓名、尾号；提交 token / revision"),
        (1,4,"8. 先写 recipient_identity_confirmed"),
        (1,3,"9. 保存本轮联系人 known=True"),
        (1,2,"10. provide_information：恢复原任务"),
        (2,3,"11. 重新认证、解析、风险与预览"),
        (2,3,"12. 小额策略再次认证 / 风险 / 预览复验"),
        (2,4,"13. 写入策略授权及执行前审计"),
        (2,3,"14. execute：同一个幂等键"),
        (2,3,"15. lookup：查询同一个幂等键"),
        (2,4,"16. status_checked / completed"),
        (2,0,"17. SUCCESS；活期 50000 → 47112 元；联系人已保存"),
    ]
    for n,(start,end,label) in enumerate(messages):
        y = 238+n*60
        x1,x2 = participants[start][0],participants[end][0]
        color = PURPLE if n < 6 else TEAL
        c.line([(x1,y),(x2,y)],color,2,dashed=end<start)
        c.text((x1+x2)/2,y-11,label,16,color,True,backing=True)
    c.rect(45,1260,1530,130,"#eee8fb","#d7caed")
    c.text(68,1293,"边界与中断规则",21,INK,bold=True)
    c.text(68,1322,"• 金额 ≥ 3000 元：身份核对后继续展示转账摘要，另行确认；不会因身份核对直接付款。",18)
    c.text(68,1349,"• 取消首次核对：不保存、不扣款。身份确认后的付款若失败，已保存联系人可能保留。",18)
    c.text(68,1376,"• 认证、风险、余额或审计不通过时中断；R4 不自动执行，UNKNOWN 只查询原幂等键。",18)
    c.save("请求时序图")


def state_diagram():
    c=Canvas(1620,1530,"核心任务状态图")
    c.text(45,53,"任务状态图｜核心变更任务的主要流转",31,INK,bold=True)
    c.text(45,88,"简化图 · 重复认证与部分异常分支省略；完整 Mermaid 见《任务状态图.md》",18)
    # Draw connecting paths first, so node text remains unobscured.
    c.line([(810,260),(810,520)],PURPLE)
    c.text(810,496,"生成服务端预览",17,PURPLE,True,backing=True)
    c.line([(810,300),(270,300),(270,345)],PURPLE)
    c.text(425,289,"字段缺失 / 重名 / 首次身份",16,PURPLE,True,backing=True)
    c.line([(810,300),(1350,300),(1350,345)],PURPLE)
    c.text(1170,289,"认证不足",16,PURPLE,True,backing=True)
    c.line([(80,410),(42,410),(42,205),(620,205)],GRAY,dashed=True)
    c.text(310,194,"补充信息后重新校验",16,GRAY,True,backing=True)
    c.line([(1540,410),(1580,410),(1580,205),(1000,205)],GRAY,dashed=True)
    c.text(1300,194,"恢复认证后重新校验",16,GRAY,True,backing=True)
    c.line([(810,650),(810,800)],TEAL)
    c.text(810,735,"确认且认证 / 风险 / 摘要复验通过",16,TEAL,True,backing=True)
    c.line([(1000,585),(1090,585),(1090,670),(1160,670)],PURPLE)
    c.text(1085,565,"确认后为 R4",16,PURPLE,True,backing=True)
    c.line([(620,585),(270,585),(270,800)],GRAY)
    c.text(385,572,"取消可取消的任务",16,GRAY,True,backing=True)
    c.line([(1540,670),(1580,670),(1580,1165),(1560,1165)],GRAY)
    c.text(1535,962,"模拟拒绝",16,GRAY,True,backing=True)
    result_centers=[190,500,810,1120,1430]
    c.line([(810,930),(810,1010)],TEAL,arrow=False)
    c.line([(190,1010),(1430,1010)],TEAL,arrow=False)
    for x,label in zip(result_centers,["成功","已登记计划","部分成功","结果不明","已知失败"]):
        c.line([(x,1010),(x,1100)],TEAL)
        c.text(x,1072,label,16,TEAL,True,backing=True)
    # UNKNOWN refresh paths deliberately do not return to PROCESSING.
    c.line([(1120,1230),(1120,1290),(190,1290),(190,1230)],GRAY,dashed=True)
    c.line([(1120,1290),(1430,1290),(1430,1230)],GRAY,dashed=True)
    c.text(810,1324,"UNKNOWN → 只按原幂等键查询 → 得到成功或失败；不再次调用 execute",18,GRAY,True,backing=True)
    c.node(620,150,380,110,"DRAFT",["请求创建与初始校验"],"#eee8fb",23)
    c.node(80,345,380,130,"AWAITING_INFORMATION",["等待补充信息 / 明确收款对象","首次核对前不保存、不扣款"],size=20)
    c.node(1160,345,380,130,"AWAITING_AUTHENTICATION",["等待达到所需认证等级","换渠道会使旧确认失效"],size=18)
    c.node(620,520,380,130,"AWAITING_CONFIRMATION",["等待当前摘要确认","过期或摘要变化时不执行旧条件"],size=19)
    c.node(1160,600,380,140,"UNDER_REVIEW",["暂停自动执行，进入模拟审核","没有自动批准后付款的路径"],"#f5eafb",21)
    c.node(80,800,380,130,"CANCELLED",["停止未执行 / 可取消操作","不自动撤销已完成交易"],"#f0edf5",23)
    c.node(620,800,380,130,"PROCESSING",["执行前审计 → 幂等提交","查询同一键，结果决定最终状态"],"#e8f4f2",23)
    result_nodes=[
        (60,"SUCCESS",["查询到成功","如实展示凭证"],"#e8f4f2"),
        (370,"SCHEDULED",["仅登记计划","不等于已付款"],"#ffffff"),
        (680,"PARTIAL_SUCCESS",["部分步骤已完成","不重提已成功部分"],"#fff6e9"),
        (990,"UNKNOWN",["已提交但结果不明","只查原幂等键"],"#f5edf9"),
        (1300,"FAILED",["校验 / 审核 / 执行失败","不宣称成功"],"#fbeff0"),
    ]
    for x,title,lines,fill in result_nodes:
        c.node(x,1100,260,130,title,lines,fill,18)
    c.rect(45,1370,1530,125,"#eee8fb","#d7caed")
    c.text(68,1403,"图中省略的明确路径与限制",21,INK,bold=True)
    c.text(68,1432,"• 普通查询校验后可直接 SUCCESS；网页小额策略或已确认范围授权可从校验阶段直接进入 PROCESSING。",18)
    c.text(68,1459,"• R4 查询先满足增强认证再提交审核；取消、确认过期和工具失败还可从其他待执行状态结束任务。",18)
    c.text(68,1486,"• PROCESSING 可能保持处理中；SCHEDULED、UNDER_REVIEW 和 UNKNOWN 均不等于执行成功。",18)
    c.save("任务状态图")


def extract_mermaid():
    source=(ROOT/"docs"/"系统架构.md").read_text(encoding="utf-8")
    blocks=re.findall(r"```mermaid\s*\n([\s\S]*?)\n```",source)
    definitions=[
        ("请求时序图","sequenceDiagram","该图原样提取自系统架构文档，包含完整首次身份核对成功路径。SVG/PNG 为便于直接查看的简化版。"),
        ("任务状态图","stateDiagram-v2","该图原样提取自系统架构文档，包含主要任务状态与异常分支。SVG/PNG 为便于直接查看的简化版。"),
    ]
    for name,prefix,note in definitions:
        block=next((block for block in blocks if block.lstrip().startswith(prefix)),None)
        if block is None:
            raise ValueError("Missing architecture Mermaid block: "+prefix)
        (OUT/(name+".mmd")).write_text(block+"\n",encoding="utf-8")
        markdown=f"# {name}\n\n文档基准：2026-10-29。{note}\n\n![{name}]({name}.svg)\n\n[PNG 图片]({name}.png) · [SVG 图片]({name}.svg) · [Mermaid 源文件]({name}.mmd)\n\n## 完整可编辑图\n\n```mermaid\n{block}\n```\n\n"
        if name=="请求时序图":
            markdown += "确认身份先保存本轮合成联系人，再按原任务认证、风险、金额规则继续；后续付款失败时已保存联系人可能保留。取消首次核对不保存、不扣款。金额恰好 3000 元或以上时另行确认转账摘要。\n"
        else:
            markdown += "图中省略部分重复认证/异常分支，准确条件以 Agent 代码为准。R4 不自动批准执行；UNKNOWN 仅查询原幂等键；PROCESSING、SCHEDULED 和 UNDER_REVIEW 不能表述为付款成功。\n"
        (OUT/(name+".md")).write_text(markdown,encoding="utf-8")


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    extract_mermaid()
    sequence_diagram()
    state_diagram()
    for path in sorted(OUT.iterdir()):
        print(path)


if __name__ == "__main__":
    main()
