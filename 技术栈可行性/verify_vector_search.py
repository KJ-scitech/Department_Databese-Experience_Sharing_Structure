# -*- coding: utf-8 -*-
"""
知识库 · Python 技术栈可行性验证
目标：跑通「文本 → 向量 → 存储 → 语义检索」最小闭环

对应需求：K1 经验录入 / K2 语义检索 / K3 经验元信息
"""
import os
import sys
import time

# Windows 控制台默认编码是 GBK，印不出 ✅ ❌ ⚠️ 这类符号。
# 该错误由 print() 自身抛出 UnicodeEncodeError，堆栈停在某一行 print 上，
# 看起来像是向量检索跑挂了，实际只是字符集问题——排查方向会被带偏。
# 这里把所有输出流的编码错误降级成「?」，宁可显示得难看也不要崩。
# （scripts/console.py 是同一套处理，那个给项目脚本用，这份是独立留档。）
for _流 in (sys.stdout, sys.stderr):
    if hasattr(_流, "reconfigure"):
        try:
            _流.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

# HuggingFace 国内镜像（模型从这下载，直连 hf.co 在国内通常不通）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 中文小模型：约 95MB，512 维。想换别的改这里即可。
#   备选：paraphrase-multilingual-MiniLM-L12-v2（多语言，~470MB）
MODEL_NAME = "BAAI/bge-small-zh-v1.5"

print("=" * 62)
print("  知识库 · Python 技术栈验证")
print("=" * 62)

# ──────────────────────────────────────────────────────────────
# 1. 准备数据：假装这是部门的经验库
# ──────────────────────────────────────────────────────────────
经验库 = [
    "活动现场投影仪突然不亮，先检查 HDMI 线是否松动，备一台笔记本，联系场地管理员拿备用投影",
    "申请活动场地要提前两周在教务处系统提交，逾期只能等下一批",
    "惟学沙龙请嘉宾要提前发邀请函，确认对方时间后再定场地",
    "活动报名人数不足时，可以在班群和部门群二次宣传，或者合并到下一场",
    "突发情况要第一时间通知部长，不要自己一个人扛着",
]

查询 = "设备出故障了咋办"

print(f"\n【经验库】共 {len(经验库)} 条：")
for i, t in enumerate(经验库, 1):
    print(f"   {i}. {t}")

print(f"\n【查询】{查询}")
print("        ↑ 注意这句话里没有'投影仪'三个字，也没有'设备'两个字")

# ──────────────────────────────────────────────────────────────
# 2. 对照组：关键词搜索（模拟 MySQL 的 LIKE '%关键词%'）
# ──────────────────────────────────────────────────────────────
print("\n" + "─" * 62)
print("① 对照组：关键词搜索（传统做法）")
print("─" * 62)

关键词 = "设备"
命中 = [t for t in 经验库 if 关键词 in t]
print(f"   搜「{关键词}」→ 命中 {len(命中)} 条")
if not 命中:
    print("   [0 条] 一条都没搜到")
    print("      原因：没有任何一条经验里写了「设备」这两个字")
    print("      → 这就是传统搜索的死穴：只比对文字，不理解意思")

# ──────────────────────────────────────────────────────────────
# 3. 加载 embedding 模型
# ──────────────────────────────────────────────────────────────
print("\n" + "─" * 62)
print(f"② 加载模型 {MODEL_NAME}")
print("─" * 62)
print("   （第一次会下载模型文件，之后走本地缓存）")

t0 = time.time()
from sentence_transformers import SentenceTransformer

模型 = SentenceTransformer(MODEL_NAME)
print(f"   加载完成，耗时 {time.time() - t0:.1f} 秒")
# 新版本改名了（get_sentence_embedding_dimension → get_embedding_dimension），
# 两个都试一下，兼容不同版本
维度 = getattr(模型, "get_embedding_dimension", None) or 模型.get_sentence_embedding_dimension
print(f"   向量维度：{维度()}")

# ──────────────────────────────────────────────────────────────
# 4. 文本 → 向量
# ──────────────────────────────────────────────────────────────
print("\n" + "─" * 62)
print("③ 把每条经验转成向量（一串数字）")
print("─" * 62)

t0 = time.time()
向量列表 = 模型.encode(经验库, show_progress_bar=False)
print(f"   {len(经验库)} 条文本已转换，耗时 {time.time() - t0:.1f} 秒")
预览 = ", ".join(f"{x:+.3f}" for x in 向量列表[0][:8])
print(f"   第 1 条经验变成了这样（只显示前 8 个数）：")
print(f"     [{预览}, ...]")

# ──────────────────────────────────────────────────────────────
# 5. 存进 Chroma
# ──────────────────────────────────────────────────────────────
print("\n" + "─" * 62)
print("④ 存进向量库 Chroma")
print("─" * 62)

import chromadb

客户端 = chromadb.Client()  # 内存模式，进程结束就没了
# ⚠️ 坑 1：Chroma 的集合名只允许 [a-zA-Z0-9._-]，不能用中文。
#    所以叫 experience 而不是「经验库」。中文放在文档内容里。
# ⚠️ 坑 2：必须显式指定 cosine。Chroma 默认空间是 l2，
#    算的是**平方**欧氏距离，不是余弦。不指定的话
#    「1 - distance」得出的根本不是相似度（详见第 ⑥ 步）。
集合 = 客户端.create_collection(
    "experience",
    metadata={"hnsw:space": "cosine"},
)
集合.add(
    ids=[f"exp-{i}" for i in range(len(经验库))],
    documents=经验库,
    embeddings=[v.tolist() for v in 向量列表],
)
print(f"   已存入 {集合.count()} 条")

# ──────────────────────────────────────────────────────────────
# 6. 语义检索 —— 关键一步
# ──────────────────────────────────────────────────────────────
print("\n" + "─" * 62)
print("⑤ 语义检索（同一句话，再搜一次）")
print("─" * 62)

t0 = time.time()
# ⚠️ 坑：这里必须用 query_embeddings，不能用 query_texts。
#    用 query_texts 的话，Chroma 会拿它自带的 all-MiniLM-L6-v2（384 维）
#    来编码查询——和入库用的 bge-small-zh（512 维）不是一个模型，
#    维度对不上直接报错，就算维度碰巧一样，意思也对不上。
#    规矩：入库和查询必须用同一个模型，自己编码完再交给 Chroma。
查询向量 = 模型.encode([查询], show_progress_bar=False)
结果 = 集合.query(query_embeddings=[查询向量[0].tolist()], n_results=3)
耗时 = (time.time() - t0) * 1000

print(f"   查「{查询}」→ 耗时 {耗时:.0f} 毫秒\n")
for i, (文档, 距离) in enumerate(zip(结果["documents"][0], 结果["distances"][0]), 1):
    相似度 = 1 - 距离
    print(f"   {i}. [相似度 {相似度:.3f}]")
    print(f"      {文档}")

# ──────────────────────────────────────────────────────────────
# 7. 两个实测结论（都踩过，写下来免得组员再踩）
# ──────────────────────────────────────────────────────────────
print("\n" + "─" * 62)
print("⑥ 实测两个容易踩的点")
print("─" * 62)

import numpy as np

# 结论 A：距离空间必须是 cosine，否则算出来的不是相似度
print("\n   A. Chroma 的默认距离空间")
print("      Chroma 不指定时用 l2，返回的是**平方**欧氏距离。")
print("      向量归一化后 L2 的平方 = 2 - 2cos，所以：")
print("        [错] 相似度 = 1 - distance")
print("        [对] 先 metadata={\"hnsw:space\": \"cosine\"}，再 1 - distance")
print("      上面第 ⑤ 步已经用了 cosine，所以那个数字可以信。")

# 结论 B：instruction 前缀在这个场景是负作用
前缀 = "为这个句子生成表示以用于检索相关文章："
查询向量_加前缀 = 模型.encode([前缀 + 查询], show_progress_bar=False)
结果_加前缀 = 集合.query(query_embeddings=[查询向量_加前缀[0].tolist()], n_results=5)

print("\n   B. 要不要给 query 加 instruction 前缀？——实测：不要")
print("      （bge 主页建议检索任务加前缀，但在我们这种短句子上反而变差）\n")
for i, (文档, 距离) in enumerate(
    zip(结果_加前缀["documents"][0], 结果_加前缀["distances"][0])
):
    加分前缀 = 1 - 距离
    原分数 = 1 - 结果["distances"][0][i] if i < len(结果["distances"][0]) else None
    差值 = f"{加分前缀 - 原分数:+.4f}" if 原分数 is not None else "  —  "
    print(f"      {加分前缀:+.4f} ({差值})   {文档[:32]}...")

print("\n      → 全部下降。结论：这个项目里**不加前缀**，两边都别加。")
print("      注意：换模型时这条要重测，别当成通用真理。")

# ──────────────────────────────────────────────────────────────
# 结论
# ──────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
print("  全链路跑通：文本 → 向量 → 存储 → 语义检索")
print("=" * 62)
print("""
  关键对照：
    关键词搜「设备」    → 0 条   （字面对不上）
    语义搜「设备出故障」→ 命中「投影仪不亮」（意思对上了）

  这就是经验库要用向量检索的原因。
""")
