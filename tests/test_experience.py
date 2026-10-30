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


# ── 枚举接口（前端下拉框的唯一来源） ───────────────────────────

def test_meta_lists_enums(seeded):
    """★ /api/meta 必须能访问到。

    这条挂在 experience 文件里不是随便放的：经验路由先注册了 GET /{exp_id}，
    谁要是哪天图省事把 /meta 挪进那个 router，它就会被通配路径吞掉，
    表现为「查类型列表返回 404，还说 exp-999 不存在」。这个用例会立刻红。
    """
    resp = seeded.get("/api/meta")
    assert resp.status_code == 200, "/api/meta 被 /{exp_id} 吞了，检查它挂在哪个 router"

    body = resp.json()
    assert body["experience_types"] == ["牢骚", "问题", "笔记", "解答"]
    for key in ("participation_roles", "member_status", "activity_status"):
        assert body.get(key), f"{key} 是空的，前端下拉框会没有选项"


# ── 标签规范化（入口拦，不靠事后补救） ─────────────────────────

def test_create_rejects_tag_with_comma(seeded):
    """★ 标签里带逗号要当场拒掉。

    逗号是 Chroma 存储标签时的分隔符（拼成串存、按逗号拆回）。
    不拦的话「设备,投影」会**静默变成两个标签**——不报错、不丢数据，
    就是悄悄地多了一个，事后根本看不出是哪来的。
    """
    resp = seeded.post("/api/experience", json={"content": "标签里带逗号", "tags": ["设备,投影"]})
    assert resp.status_code == 400
    assert "逗号" in resp.json()["detail"]


def test_tags_are_trimmed_deduped_and_ordered(seeded):
    """去空白、丢空串、保序去重——顺序必须是输入顺序，不能靠 set。"""
    created = seeded.post("/api/experience", json={
        "content": "验收测试：标签规范化，写完就删。",
        "tags": ["  设备 ", "设备", "投影", ""],
    })
    assert created.status_code == 200
    exp_id = created.json()["exp_id"]

    assert created.json()["metadata"]["tags"] == ["设备", "投影"]

    seeded.delete(f"/api/experience/{exp_id}")


def test_create_rejects_overlong_tag(seeded):
    resp = seeded.post("/api/experience", json={"content": "超长标签", "tags": ["长" * 33]})
    assert resp.status_code == 400


# ── K8 改标签（PATCH）─────────────────────────────────────────

def test_patch_tags_keeps_other_metadata(seeded):
    """改标签不能动别的元信息。

    正文、作者、活动 id、来源都不在这次改动的范围内，改完必须一个没变。
    （Chroma 的 update 对 metadata 是**按键合并**的——实测只传 {"tags": …}
    时别的键也还在。所以这条守的不是某个实现细节，而是「PATCH 只改标签」
    这个对外契约：真要是哪天有人顺手把正文也写进去，这里会红。）


    作者和活动 id 从 exp-1 上取，不写死数字：种子的 id 以后万一改了，
    这条测试不该跟着红。
    """
    seed_meta = seeded.get("/api/experience/exp-1").json()["metadata"]

    created = seeded.post("/api/experience", json={
        "content": "验收测试：改标签丢元信息的回归。写完就删。",
        "type": "笔记",
        "tags": ["老标签"],
        "author_id": seed_meta["author_id"],
        "activity_id": seed_meta["activity_id"],
        "source": "回归测试",
    })
    assert created.status_code == 200
    exp_id = created.json()["exp_id"]
    before = created.json()["metadata"]

    try:
        patched = seeded.patch(f"/api/experience/{exp_id}", json={"tags": ["新标签"]})
        assert patched.status_code == 200
        after = patched.json()["metadata"]

        assert after["tags"] == ["新标签"], "PATCH 是整份替换，不是追加"
        for key in ("author_id", "activity_id", "type", "source", "created_at"):
            assert after.get(key) == before.get(key), (
                f"{key} 被写丢了。Chroma 的 update 是整份替换 metadata，"
                "PATCH 必须「读出整份 → 只改 tags → 整份写回」"
            )
    finally:
        seeded.delete(f"/api/experience/{exp_id}")


def test_patch_rejects_extra_fields(seeded):
    """★ 只开放 tags，多传字段要 422，不能假装没看见。

    Pydantic 默认**静默忽略**多余字段：不加 extra="forbid" 的话，
    {"tags": [...], "content": "偷改正文"} 会返回 200 而正文没改，
    调用方以为改成功了。这条测试同时验证「真的没改」。
    """
    before = seeded.get("/api/experience/exp-1").json()

    resp = seeded.patch("/api/experience/exp-1", json={"tags": ["设备"], "content": "偷改正文"})
    assert resp.status_code == 422, "多余字段被静默忽略了，ExperienceTagsPatch 要 extra='forbid'"

    after = seeded.get("/api/experience/exp-1").json()
    assert after["content"] == before["content"], "正文不该被这条请求动过"
    assert after["metadata"]["tags"] == before["metadata"]["tags"], "422 的请求不该改到标签"


def test_patch_missing_experience_returns_404(seeded):
    resp = seeded.patch("/api/experience/exp-999999", json={"tags": ["设备"]})
    assert resp.status_code == 404


# ── 列表筛选 ──────────────────────────────────────────────────

def test_list_filter_by_type(seeded):
    # 种子语料实际是 解答×2 / 笔记×4 / 牢骚×2，**没有「问题」**——
    # 别照着枚举表里的四个值随便挑一个，挑到空集的话断言会以
    # 「筛选没生效」的面目出现，其实是数据里本来就没有。
    resp = seeded.get("/api/experience", params={"type": "笔记"})
    assert resp.status_code == 200

    items = resp.json()
    assert items, "种子里应该有「笔记」类经验（没有的话是 seed 数据变了）"
    assert all(item["metadata"]["type"] == "笔记" for item in items)


def test_list_total_count_is_after_filter(seeded):
    """X-Total-Count 要是**筛选后**的总数，分页器靠它算末页。

    给成全库总数的话，筛完只有 3 条也会显示 8 页，翻过去全是空的。
    """
    total_all = int(seeded.get("/api/experience", params={"limit": 1}).headers["X-Total-Count"])
    assert total_all > 1

    filtered = seeded.get("/api/experience", params={"type": "笔记", "limit": 1})
    filtered_total = int(filtered.headers["X-Total-Count"])
    assert 0 < filtered_total < total_all, (
        f"筛选后总数是 {filtered_total}、全库 {total_all}——"
        "相等说明 X-Total-Count 给的是全库总数，0 说明筛出空集了"
    )


def test_list_filter_by_tag(seeded):
    tag = seeded.get("/api/experience/exp-1").json()["metadata"]["tags"][0]

    resp = seeded.get("/api/experience", params={"tag": tag})
    assert resp.status_code == 200
    assert resp.json(), f"按标签 {tag} 一条都没筛出来"

    for item in resp.json():
        assert tag in item["metadata"]["tags"]


def test_list_without_tags(seeded):
    """without_tags 是 K8 打标前的待办清单，不能把已有标签的也算进去。"""
    created = seeded.post("/api/experience", json={
        "content": "验收测试：这条故意不打标签。写完就删。",
        "type": "笔记",
    })
    assert created.status_code == 200, f"不带标签的经验建不出来：{created.text}"
    exp_id = created.json()["exp_id"]

    try:
        resp = seeded.get("/api/experience", params={"without_tags": True, "limit": 200})
        assert resp.status_code == 200

        ids = [item["exp_id"] for item in resp.json()]
        assert exp_id in ids, "刚建的无标签经验没被筛出来"
        assert all(not item["metadata"]["tags"] for item in resp.json()), "混进了有标签的"
    finally:
        seeded.delete(f"/api/experience/{exp_id}")


def test_list_q_is_literal_not_semantic(seeded):
    """q 是字面包含（给筛选条用的），语义召回走 /search——两条路分工不同。"""
    hits = seeded.get("/api/experience", params={"q": "投影仪"}).json()
    assert hits
    assert all("投影仪" in item["content"] for item in hits)

    # 和 /keyword 同一个前提：字面搜「设备出故障了咋办」一条都不该有。
    assert seeded.get("/api/experience", params={"q": "设备出故障了咋办"}).json() == []


def test_list_filter_result_is_paginated_after_filter(seeded):
    """★ 先筛后切，不能先切后筛。

    反过来做的话：第 1 页 50 条被筛掉只剩 3 条，第 2 页从第 51 条开始，
    中间那 47 条谁也看不到——分页会静默漏数据。
    """
    page = seeded.get("/api/experience", params={"q": "投影仪", "limit": 1}).json()
    assert len(page) == 1, "筛完只剩 1 条，却按全库切了片"
    assert "投影仪" in page[0]["content"]
