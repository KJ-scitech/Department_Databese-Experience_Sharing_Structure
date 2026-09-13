"""经验检索线（K1 / K2 / K3）的验收测试。

对应的验收标准（任务拆解.md 3.4）：

    检索「设备出故障」，能召回「投影仪不亮」条目。

以及 3.5 里那三条踩过的坑——它们在下面都有对应的回归测试，
谁要是哪天把代码改回错的做法，这些用例会红。
"""


# ── 自检 ──────────────────────────────────────────────────────

def test_health_ok(seeded):
    """服务起来了，数据库通，向量库有料。"""
    resp = seeded.get("/api/health")
    assert resp.status_code == 200

    body = resp.json()
    assert body["ok"] is True, f"数据库没连上：{body['database']}"
    assert body["experience_count"] == 8, "经验条数不是 8，是不是没跑 scripts.seed"
    assert body["embedding_dim"] == 512, "向量维度不对，模型可能换了"


# ── K2 语义检索（本线的核心验收） ──────────────────────────────

def test_semantic_search_recalls_projector(seeded):
    """★ 验收标准：问「设备出故障了咋办」，第一条是「投影仪不亮」。

    这条是整个项目的核心价值——按字面根本搜不到，
    两条文本一个共同词都没有。
    """
    resp = seeded.get("/api/experience/search", params={"q": "设备出故障了咋办"})
    assert resp.status_code == 200

    hits = resp.json()
    assert hits, "一条都没召回，向量库可能是空的"
    assert "投影仪" in hits[0]["content"], (
        f"第一条不是「投影仪不亮」，而是：{hits[0]['content'][:40]}…"
    )


def test_similarity_values_are_cosine(seeded):
    """★ 坑 1 的回归测试：相似度必须是余弦，不是 L2 平方距离。

    如果谁把 get_or_create_collection 里的 metadata={"hnsw:space": "cosine"}
    删了，Chroma 会退回 L2 空间、返回**平方**欧氏距离，
    这时算出来的「相似度」会掉到 0.13 左右——数字还在，但没意义了。

    所以这里卡一个区间：正常应当在 0.5~0.7，掉到 L2 就明显出界。
    这个错误不会抛异常，只会静默给错数字，靠人工看很难发现，
    必须靠断言钉住。
    """
    resp = seeded.get("/api/experience/search", params={"q": "设备出故障了咋办"})
    hits = resp.json()
    top = hits[0]["similarity"]

    assert 0.0 <= top <= 1.0, f"相似度不在 [0,1] 区间：{top}"
    assert 0.5 <= top <= 0.7, (
        f"首条相似度是 {top}，不在预期的 0.5~0.7。\n"
        "   若接近 0.13，多半是集合的距离空间不是 cosine（见 vectors.py 坑 1）"
    )


def test_search_respects_top_k(seeded):
    """top_k 要真的生效，不能给多少都返回全部。"""
    resp = seeded.get("/api/experience/search", params={"q": "场地怎么布置", "top_k": 2})
    assert len(resp.json()) == 2


def test_keyword_search_finds_nothing(seeded):
    """★ 坑 2 的对照：同一个问句，字面匹配一条都找不到。

    这不是 bug，是**故意**用来做对照的：
    证明「为什么非得用向量检索」——大家记笔记时的用词根本对不上。
    如果这个测试哪天开始能搜出东西了，说明语料被改过，
    那条对照演示就失效了，要回看 web/index.html 上的说明文字。
    """
    resp = seeded.get("/api/experience/keyword", params={"q": "设备出故障了咋办"})
    assert resp.status_code == 200
    assert resp.json() == [], "字面匹配居然命中了几条，对照演示的前提变了"


def test_keyword_search_does_work_on_literal_terms(seeded):
    """但字面匹配本身是好的——搜语料里真实存在的词就该有结果。

    这条和上一条一起看才有意义：
    关键词检索不是坏了，它只是在「用词对不上」时无能无力。
    """
    resp = seeded.get("/api/experience/keyword", params={"q": "投影仪"})
    hits = resp.json()
    assert hits, "连「投影仪」都搜不到，字面匹配是真的坏了"
    assert "投影仪" in hits[0]["content"]


# ── K3 元信息 ─────────────────────────────────────────────────

def test_get_experience_translates_ids_to_names(seeded):
    """双库关联：元信息里的 author_id 要能翻译成人名。

    经验本体在 Chroma，人名在 MySQL——这正是需求文档说的
    「双库并存，通过 id 关联」。翻译不出来就说明关联断了。
    """
    resp = seeded.get("/api/experience/exp-1")
    assert resp.status_code == 200

    meta = resp.json()["metadata"]
    assert meta.get("author_name"), "author_id 没能翻成人名，双库关联可能断了"
    assert meta.get("activity_name"), "activity_id 没能翻成活动名"
    assert isinstance(meta.get("tags"), list), "标签应该返回列表，不是逗号串"


def test_get_missing_experience_returns_404(seeded):
    resp = seeded.get("/api/experience/exp-999999")
    assert resp.status_code == 404


def test_list_experiences(seeded):
    resp = seeded.get("/api/experience", params={"limit": 5})
    assert resp.status_code == 200
    assert len(resp.json()) == 5


# ── K1 录入 ───────────────────────────────────────────────────

def test_create_experience_rejects_unknown_type(seeded):
    """类型不在「牢骚/问题/笔记/解答」里就要拒掉。"""
    resp = seeded.post("/api/experience", json={"content": "随便写点", "type": "瞎写的类型"})
    assert resp.status_code == 400
    assert "type" in resp.json()["detail"]


def test_create_experience_rejects_orphan_author(seeded):
    """author_id 指向不存在的人，要拒——不能造出孤儿引用。"""
    resp = seeded.post("/api/experience", json={"content": "随便写点", "author_id": 999999})
    assert resp.status_code == 400


def test_create_then_delete_experience(seeded):
    """录入一条新的，能查到，再删掉。

    这个用例会真的往向量库里写一条再删掉，
    跑完不留痕迹——所以它不会影响上面那些「经验条数 == 8」的断言。
    """
    before = seeded.get("/api/health").json()["experience_count"]

    created = seeded.post("/api/experience", json={
        "content": "验收测试写的临时经验，写完就删。",
        "type": "笔记",
        "tags": ["临时"],
    })
    assert created.status_code == 200
    exp_id = created.json()["exp_id"]

    assert seeded.get(f"/api/experience/{exp_id}").status_code == 200
    assert seeded.get("/api/health").json()["experience_count"] == before + 1

    deleted = seeded.delete(f"/api/experience/{exp_id}")
    assert deleted.status_code == 200
    assert seeded.get("/api/health").json()["experience_count"] == before
