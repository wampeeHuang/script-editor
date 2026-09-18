"""核心链路烟测：单元切分 / 两层时间轴 / 补丁脏检测 / 真实合成（豆包）。

用法：python tests/smoke.py            # 只跑离线断言
      python tests/smoke.py --live     # 追加一次真实豆包合成，须明确授权
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import model  # noqa: E402
import tts  # noqa: E402

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got}" + ("" if ok else f"  （期望 {want}）"))
    if not ok:
        FAIL.append(name)


def build(backend="doubao"):
    p = model.new_project("掼蛋指南")
    p["meta"]["backends"] = tts.default_backends()
    p["meta"]["defaultBackend"] = backend
    p["segments"] = [{"id": "s1", "title": "开场", "backend": "", "shots": []}]
    p["sentences"] = [
        {"sid": "s1", "text": "姐妹们，人齐了，我们来掼蛋！"},
        {"sid": "s1", "text": "我搓了一个网页版的小指南，"},
        {"sid": "s1", "text": "就挂在我的小红书上，点开就能看。"},
        {"sid": "s1", "text": "专门写给完全没打过的小白，"},
        {"sid": "s1", "text": "连斗地主都不会的姐妹，也能看懂。"},
    ]
    return model.normalize_project(p)


print("─" * 72)
print("① 单元切分（句末标点界）")
p = build()
units = model.unit_plan(p, "s1")
check("单元数", len(units), 3)
check("单元1 覆盖句", units[0]["sentenceIds"], [1])
check("单元2 覆盖句", units[1]["sentenceIds"], [2, 3])
check("单元3 覆盖句", units[2]["sentenceIds"], [4, 5])
check("单元2 文本", units[1]["text"], "我搓了一个网页版的小指南，就挂在我的小红书上，点开就能看。")

print("\n①b 合成文本规范化：显示不变，送去合成的补上标点")
p_np = model.normalize_project({
    "sentences": [
        {"sid": "s1", "text": "我搓了一个网页版的小指南"},          # 行尾没写逗号
        {"sid": "s1", "text": "就挂在我的小红书上，点开就能看。"},
        {"sid": "s1", "text": "专门写给完全没打过的小白，"},
        {"sid": "s1", "text": "连斗地主都不会的姐妹，也能看懂"},     # 段尾没写句号
    ]})
u_np = model.unit_plan(p_np, "s1")
check("行间缺标点 → 合成时补「，」", u_np[0]["text"],
      "我搓了一个网页版的小指南，就挂在我的小红书上，点开就能看。")
check("段尾缺标点 → 合成时补「。」", u_np[1]["text"],
      "专门写给完全没打过的小白，连斗地主都不会的姐妹，也能看懂。")
check("显示文本一个字都没动（用户怎么写就怎么显示）",
      u_np[0]["texts"], ["我搓了一个网页版的小指南", "就挂在我的小红书上，点开就能看。"])

p_edge = model.normalize_project({
    "sentences": [
        {"sid": "s1", "text": "专门写给完全没打过的小白~"},   # 语气符号收尾
        {"sid": "s1", "text": "还有斗地主都不会的姐妹。"},
        {"sid": "s1", "text": "也就是百搭牌"},                # 下一行以逗号开头
        {"sid": "s1", "text": "，两个7凑一张红心2就能变了。"},
    ]})
u_e = model.unit_plan(p_edge, "s1")
check("上一行以「~」收尾 → 不再补逗号（否则成「小白~，」）", u_e[0]["text"],
      "专门写给完全没打过的小白~还有斗地主都不会的姐妹。")
check("下一行自带逗号开头 → 不补（否则成「百搭牌，，」）", u_e[1]["text"],
      "也就是百搭牌，两个7凑一张红心2就能变了。")
check("段尾是「~」→ 不补句号（否则成「吧~。」）", model.unit_text(
      ["连斗地主都不会的姐妹，也能看懂~"], [0], True),
      "连斗地主都不会的姐妹，也能看懂~")

print("\n② 无标点输入的粘合（不许出现半句话单独合成）")
p2 = model.new_project()
p2["sentences"] = [{"sid": "s1", "text": "前面有句话。"},
                   {"sid": "s1", "text": "中间这行没有标点"},
                   {"sid": "s1", "text": "后面接上了。"}]
p2 = model.normalize_project(p2)
check("无标点行与后句粘成同一单元", [u["sentenceIds"] for u in model.unit_plan(p2, "s1")], [[1], [2, 3]])
check("无标点行绝不被单独合成", all(len(u["sentenceIds"]) > 1
      for u in model.unit_plan(p2, "s1") if "中间这行没有标点" in u["text"]), True)

print("\n③ 全段无标点 ⇒ 整段一个单元")
p3 = model.new_project()
p3["sentences"] = [{"sid": "s1", "text": "第一行没有标点"},
                   {"sid": "s1", "text": "第二行也没有"}]
p3 = model.normalize_project(p3)
check("单元数", len(model.unit_plan(p3, "s1")), 1)

print("\n④ 段尾逗号规范化（实测：输入以逗号结尾会让 TTS 输出不可预测）")
p4 = model.new_project()
p4["sentences"] = [{"sid": "s1", "text": "前面一句。"}, {"sid": "s1", "text": "结尾这句带逗号，"},]
p4 = model.normalize_project(p4)
u = model.unit_plan(p4, "s1")
check("段尾逗号 → 句号", u[1]["text"], "结尾这句带逗号。")
check("非段尾不动", u[0]["text"], "前面一句。")

print("\n⑤ 两层时间轴（骨架由单元时长构造 + 单元内字符权重细分）")
idx = model.sync_index(p, model.empty_index())
for uid, dur in (("s1u1", 2.78), ("s1u2", 5.21), ("s1u3", 4.37)):
    model.mark_synthesized(idx, uid, f"units/{idx['units'][uid]['hash']}.wav", dur, "doubao")
tl = model.compute_timeline(p, idx)
check("总时长", tl["total"], round(2.78 + 5.21 + 4.37, 3))
check("stale", tl["stale"], False)
check("句1 start", tl["sentences"][0]["start"], 0.0)
check("句1 end", tl["sentences"][0]["end"], 2.78)
check("句2 start == 单元2 起点", tl["sentences"][1]["start"], 2.78)
check("句5 end == 总时长", tl["sentences"][4]["end"], round(2.78 + 5.21 + 4.37, 3))
mono = all(tl["sentences"][k]["end"] <= tl["sentences"][k + 1]["start"] + 1e-6
           for k in range(4))
check("单调不重叠（A4）", mono, True)
# 单元2 内两句按字符权重切：不会恰好平分
s2 = tl["sentences"][1]
s3 = tl["sentences"][2]
check("单元内细分非平分", round(s3["start"] - s2["start"], 3) != round(s3["end"] - s3["start"], 3), True)

print("\n⑥ 补丁：逗号改句号 → 重新切分必须被捕获（按哈希比对，非下标）")
ops = [{"op": "replaceText", "i": 2, "text": "我搓了一个网页版的小指南。"}]
p_after = model.apply_patch(p, ops)
d = model.diff_changed_segments(p, p_after)
check("受影响段", d["segments"], ["s1"])
check("改后单元数（逗号变句号 ⇒ 多切出一个单元）", len(model.unit_plan(p_after, "s1")), 4)
check("有单元被标记新增", len(d["added"]) > 0, True)
check("句2 文本已改", model.sentences_of(p_after, "s1")[1]["text"], "我搓了一个网页版的小指南。")

print("\n⑦ 增量：按内容哈希判定，重切分不误伤未改内容")
idx2 = model.sync_index(p_after, json.loads(json.dumps(idx)))
dirty = model.dirty_units(p_after, idx2)
check("脏单元数（仅新文本 2 个）", len(dirty), 2)
check("s1u1 未改 → clean", idx2["units"]["s1u1"]["status"], "clean")
check("s1u4 内容未改、仅位置平移 → clean（这正是修掉的 bug）",
      idx2["units"]["s1u4"]["status"], "clean")
check("脏单元清单", [d["uid"] for d in dirty], ["s1u2", "s1u3"])
resp = model.apply_patch(p_after, [{"op": "replaceText", "i": 2,
                                   "text": "我搓了一个网页版的小指南，"}])
# 关键：必须用 idx2（已被上一步 sync 动过）继续，才是真实时序。
# 原来这里传的是 idx 的旧副本，等于绕开了缓存回收路径 —— 测试假通过，
# 线上表现为「撤销回原文后仍提示待合成，要重新花钱」。
check("改一版之后，原文的音频缓存仍在",
      any(r.get("audio") for r in idx2["cache"].values()), True)
# sync_index 是原地更新的（它会重写 index["units"]），所以这里必须传副本 ——
# 否则后面 ⑧ 的估算会拿到「回退后的计划」，与仍在改后状态的 p_after 对不上。
idx3 = model.sync_index(resp, json.loads(json.dumps(idx2)))
check("改回原文 ⇒ 命中缓存，全部 clean",
      [u["status"] for u in idx3["units"].values()], ["clean", "clean", "clean"])

print("\n⑧ 成本预估（单价来自配置，工具不猜）")
est = model.estimate(p_after, idx2, "doubao")
check("待合成单元数 == 脏单元数", est["units"], len(dirty))
check("字符数为正", est["chars"] > 0, True)
check("单价未配置 ⇒ 费用未知", est["estimatedCost"], None)
p_price = json.loads(json.dumps(p_after))
next(b for b in p_price["meta"]["backends"] if b["id"] == "doubao")["pricePer1kChars"] = 50
est2 = model.estimate(p_price, idx2, "doubao")
check("配置单价后按口径计算", est2["estimatedCost"], round(est["chars"] / 1000 * 50, 4))

print("\n⑨ 后端健康检查（不回显密钥值）")
h = tts.health(p)
check("ffmpeg 就绪", h["ffmpeg"], True)
check("豆包密钥就绪", h["doubaoKey"], True)
check("只保留本机朗读与豆包", sorted(b["id"] for b in h["backends"]), ["browser", "doubao"])
check("退役的本机后端不占位",
      [b["id"] for b in h["backends"] if b["id"] in {"indextts", "cosyvoice3"}], [])

if "--live" in sys.argv:
    print("\n⑩ 真实合成（豆包 · 单元 s1u1）")
    out = ROOT / "_runtime" / "smoke" / "s1u1.wav"
    r = tts.synthesize(p, "doubao", model.unit_plan(p, "s1")[0]["text"], out, mode="master")
    print(f"  [{'PASS' if r['ok'] and r['duration'] else 'FAIL'}] master: {r['path']}")
    print(f"         时长 {r['duration']}s  等待 {r['waited']}s  "
          f"字节 {out.stat().st_size if out.exists() else 0}")
    if not (r["ok"] and r["duration"]):
        FAIL.append("live master")
    out2 = ROOT / "_runtime" / "smoke" / "s1u1.preview.mp3"
    r2 = tts.synthesize(p, "doubao", model.unit_plan(p, "s1")[0]["text"], out2, mode="preview")
    print(f"  [{'PASS' if r2['ok'] else 'FAIL'}] preview: 首包 {r2['firstChunk']}s "
          f"总 {r2['total']}s  时长 {r2['duration']}s")
    if not r2["ok"]:
        FAIL.append("live preview")

print()
print("─" * 72)
print(f"结果：{'全部通过' if not FAIL else '失败 ' + str(FAIL)}")
sys.exit(1 if FAIL else 0)
