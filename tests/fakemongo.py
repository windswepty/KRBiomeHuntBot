"""실 DB 를 건드리지 않고 Mongo 경로를 실행하기 위한 가짜 드라이버.

`_db()` 가 pymongo 호출을 **스레드로** 넘기므로, 진짜 동시성을 재현하려면
연산이 스레드 안에서 돌면서 문서 단위로 직렬화되어야 합니다. 그래서
`threading.RLock` 을 문서마다 하나씩 두고, 연산 전체를 그 락 안에서 처리합니다.

⚠️ 연산자가 하나라도 빠지면 그 조건이 **통째로 무시되어** 테스트가 거짓
통과합니다. 실제로 `$lte` 누락으로 상한 검증이 실제와 다르게 나온 적이
있습니다. 그래서 모르는 연산자를 만나면 조용히 넘기지 않고 **터뜨립니다.**
"""
import copy
import re
import threading

try:
    from bson import ObjectId as _ObjectId
except ImportError:            # pymongo 가 없으면 문자열로 대체
    _ObjectId = None


class UnknownOperator(Exception):
    pass


try:
    from pymongo.errors import DuplicateKeyError as _PyDup
except ImportError:
    _PyDup = Exception


class DuplicateKey(_PyDup):
    """**진짜** pymongo 예외를 상속합니다.

    코드가 `except DuplicateKeyError` 로 거르는데 대역이 다른 예외를 던지면,
    그 분기는 한 번도 실행되지 않은 채 테스트가 통과합니다.
    """

    def __init__(self, _id):
        Exception.__init__(
            self,
            f"E11000 duplicate key error collection: _id_ dup key: {_id!r}")


def _new_oid():
    return _ObjectId() if _ObjectId is not None else None


# ── 질의 ─────────────────────────────────────────────────────────────

def _get(doc, path):
    """`a.b.c` 점 표기로 값을 꺼냅니다. 없으면 (None, False)."""
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def _eq(value, want):
    """Mongo 의 '같음'. 필드가 배열이면 **원소 중 하나만 같아도** 참입니다.

    이 규칙을 빼먹으면 `claimed` 같은 배열에 대한 검사가 통째로 무너집니다.
    """
    if isinstance(value, list) and not isinstance(want, list):
        return any(v == want for v in value)
    return value == want


def _cmp_ok(value, cond):
    """`{"$gte": 5}` 같은 조건 하나를 판정합니다."""
    if not isinstance(cond, dict) or not any(
            k.startswith("$") for k in cond):
        return _eq(value, cond)

    for op, want in cond.items():
        if op == "$ne":
            # 배열이면 '원소로 포함하지 않음' 입니다
            if _eq(value, want):
                return False
        elif op == "$gte":
            if value is None or not value >= want:
                return False
        elif op == "$lte":
            if value is None or not value <= want:
                return False
        elif op == "$gt":
            if value is None or not value > want:
                return False
        elif op == "$lt":
            if value is None or not value < want:
                return False
        elif op == "$in":
            if not any(_eq(value, w) for w in want):
                return False
        elif op == "$nin":
            if any(_eq(value, w) for w in want):
                return False
        elif op == "$exists":
            # 호출부는 _exists 표식을 따로 넘깁니다 (아래 _match 참고)
            raise UnknownOperator("$exists 는 _match 에서 처리해야 합니다")
        elif op == "$elemMatch":
            if not isinstance(value, list):
                return False
            if not any(_match(v if isinstance(v, dict) else {}, want)
                       for v in value):
                return False
        elif op == "$regex":
            flags = re.I if "i" in cond.get("$options", "") else 0
            if value is None or not re.search(want, str(value), flags):
                return False
        elif op == "$options":
            continue                    # $regex 와 함께 처리됨
        else:
            raise UnknownOperator(f"질의 연산자 미구현: {op}")
    return True


def _match(doc, query):
    for key, cond in query.items():
        if key.startswith("$"):
            raise UnknownOperator(f"최상위 질의 연산자 미구현: {key}")
        value, present = _get(doc, key)
        if isinstance(cond, dict) and "$exists" in cond:
            if bool(cond["$exists"]) != present:
                return False
            rest = {k: v for k, v in cond.items() if k != "$exists"}
            if rest and not _cmp_ok(value, rest):
                return False
            continue
        if not _cmp_ok(value, cond):
            return False
    return True


# ── 갱신 ─────────────────────────────────────────────────────────────

def _set_path(doc, path, value):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _positional_target(doc, query, path):
    """`items.$.stock` — 질의의 $elemMatch 에 맞은 **첫 원소**를 고릅니다."""
    field, _, rest = path.partition(".$.")
    arr = doc.get(field)
    if not isinstance(arr, list):
        return None
    cond = query.get(field, {})
    inner = cond.get("$elemMatch") if isinstance(cond, dict) else None
    for elem in arr:
        if not isinstance(elem, dict):
            continue
        if inner is None or _match(elem, inner):
            return elem, rest
    return None


def _apply_update(doc, update, query, upserted):
    for op, body in update.items():
        if op == "$set":
            for k, v in body.items():
                if ".$." in k:
                    hit = _positional_target(doc, query, k)
                    if hit:
                        hit[0][hit[1]] = v
                else:
                    _set_path(doc, k, v)
        elif op == "$inc":
            for k, v in body.items():
                if ".$." in k:
                    hit = _positional_target(doc, query, k)
                    if hit:
                        hit[0][hit[1]] = (hit[0].get(hit[1], 0) or 0) + v
                else:
                    cur, _ = _get(doc, k)
                    _set_path(doc, k, (cur or 0) + v)
        elif op == "$setOnInsert":
            if upserted:
                for k, v in body.items():
                    _set_path(doc, k, v)
        elif op == "$push":
            for k, v in body.items():
                cur, _ = _get(doc, k)
                arr = list(cur or [])
                if isinstance(v, dict) and "$each" in v:
                    arr.extend(v["$each"])
                    if "$slice" in v:
                        n = v["$slice"]
                        arr = arr[n:] if n < 0 else arr[:n]
                    extra = {k2 for k2 in v if k2 not in ("$each", "$slice")}
                    if extra:
                        raise UnknownOperator(f"$push 하위 미구현: {extra}")
                else:
                    arr.append(v)
                _set_path(doc, k, arr)
        elif op == "$unset":
            for k in body:
                parts = k.split(".")
                cur = doc
                for p in parts[:-1]:
                    cur = cur.get(p, {})
                cur.pop(parts[-1], None)
        else:
            raise UnknownOperator(f"갱신 연산자 미구현: {op}")


def _project(doc, projection):
    if not projection:
        return doc
    if all(v == 0 for v in projection.values()):
        return {k: v for k, v in doc.items() if k not in projection}
    keep = set(projection) | {"_id"}
    return {k: v for k, v in doc.items() if k in keep}


# ── 커서 ─────────────────────────────────────────────────────────────

class Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, key, direction=1):
        if isinstance(key, list):
            for k, d in reversed(key):
                self._rows.sort(key=lambda r: _sortable(r.get(k)),
                                reverse=(d < 0))
        else:
            self._rows.sort(key=lambda r: _sortable(r.get(key)),
                            reverse=(direction < 0))
        return self

    def skip(self, n):
        self._rows = self._rows[n:]
        return self

    def limit(self, n):
        if n:
            self._rows = self._rows[:n]
        return self

    def __iter__(self):
        return iter(self._rows)


def _sortable(v):
    """None 과 숫자/문자가 섞여도 정렬이 터지지 않게 합니다."""
    if v is None:
        return (0, 0, "")
    if isinstance(v, bool):
        return (1, int(v), "")
    if isinstance(v, (int, float)):
        return (1, v, "")
    return (2, 0, str(v))


class _Result:
    def __init__(self, matched=0, modified=0, upserted_id=None,
                 deleted=0, upserted_ids=None, inserted_id=None):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_id = upserted_id
        self.inserted_id = inserted_id
        self.deleted_count = deleted
        self.upserted_ids = upserted_ids or {}
        self.acknowledged = True


AFTER = "after"
BEFORE = "before"


class FakeCollection:
    """문서 단위 락으로 진짜 동시성을 재현합니다.

    Mongo 는 단일 문서 연산이 원자적입니다. 그 성질이 이 코드베이스의
    동시성 안전을 떠받치고 있으므로, 대역도 같은 보장을 줘야 합니다.
    """

    def __init__(self, name="col"):
        self.name = name
        self.docs = {}
        self._seq = 0
        self._locks = {}
        self._guard = threading.Lock()
        self.op_counts = {}

    # 내부 -------------------------------------------------------
    def _lock_for(self, _id):
        with self._guard:
            return self._locks.setdefault(_id, threading.RLock())

    def _tally(self, op):
        with self._guard:
            self.op_counts[op] = self.op_counts.get(op, 0) + 1

    def _scan(self, query):
        return [d for d in list(self.docs.values()) if _match(d, query)]

    # pymongo 흉내 ------------------------------------------------
    def create_index(self, *a, **k):
        return "idx"

    def find_one(self, query, projection=None):
        self._tally("find_one")
        hits = self._scan(query or {})
        if not hits:
            return None
        return copy.deepcopy(_project(hits[0], projection))

    def find(self, query=None, projection=None):
        self._tally("find")
        rows = [copy.deepcopy(_project(d, projection))
                for d in self._scan(query or {})]
        return Cursor(rows)

    def count_documents(self, query):
        self._tally("count_documents")
        return len(self._scan(query or {}))

    def insert_one(self, doc):
        self._tally("insert_one")
        # 진짜 Mongo 는 _id 가 없으면 ObjectId 를 만들어 넣습니다.
        # 이걸 빼먹으면 _id=None 끼리 충돌해 E11000 이 납니다 (거짓 실패).
        _id = doc.get("_id")
        with self._guard:
            if _id is None:
                self._seq += 1
                _id = _new_oid() or f"oid{self._seq:08d}"
        with self._lock_for(_id):
            if _id in self.docs:
                raise DuplicateKey(_id)
            new = copy.deepcopy(doc)
            new["_id"] = _id
            self.docs[_id] = new
        return _Result(inserted_id=_id, upserted_id=_id)

    def replace_one(self, query, doc, upsert=False):
        self._tally("replace_one")
        _id = query.get("_id")
        with self._lock_for(_id):
            hits = self._scan(query)
            if hits:
                new = copy.deepcopy(doc)
                new["_id"] = hits[0]["_id"]
                self.docs[new["_id"]] = new
                return _Result(matched=1, modified=1)
            if upsert:
                new = copy.deepcopy(doc)
                new.setdefault("_id", _id)
                self.docs[new["_id"]] = new
                return _Result(upserted_id=new["_id"])
        return _Result()

    def update_one(self, query, update, upsert=False):
        self._tally("update_one")
        _id = query.get("_id")
        with self._lock_for(_id):
            hits = self._scan(query)
            if hits:
                doc = self.docs[hits[0]["_id"]]
                before = copy.deepcopy(doc)
                _apply_update(doc, update, query, upserted=False)
                return _Result(matched=1, modified=int(before != doc))
            if upsert:
                # 필터의 '같음' 조건만 씨앗으로 씁니다 (Mongo 와 같은 규칙)
                doc = {k: v for k, v in query.items()
                       if not isinstance(v, dict)}
                doc.setdefault("_id", _id if _id is not None else _new_oid())
                # 필터에는 안 걸렸는데 같은 _id 가 이미 있으면, Mongo 는
                # 삽입을 시도하다 중복 키로 실패합니다. 조용히 덮어쓰면
                # 중복 방지 로직이 통째로 무력화됩니다.
                if doc["_id"] in self.docs:
                    raise DuplicateKey(doc["_id"])
                self.docs[doc["_id"]] = doc
                _apply_update(doc, update, query, upserted=True)
                return _Result(upserted_id=doc["_id"])
        return _Result()

    def find_one_and_update(self, query, update, upsert=False,
                            return_document=None, projection=None,
                            sort=None):
        self._tally("find_one_and_update")
        _id = query.get("_id")
        with self._lock_for(_id):
            hits = self._scan(query)
            if hits:
                doc = self.docs[hits[0]["_id"]]
                before = copy.deepcopy(doc)
                _apply_update(doc, update, query, upserted=False)
                out = doc if return_document == AFTER else before
                return copy.deepcopy(_project(out, projection))
            if upsert:
                doc = {k: v for k, v in query.items()
                       if not isinstance(v, dict)}
                doc.setdefault("_id", _id if _id is not None else _new_oid())
                if doc["_id"] in self.docs:
                    raise DuplicateKey(doc["_id"])
                self.docs[doc["_id"]] = doc
                _apply_update(doc, update, query, upserted=True)
                if return_document == AFTER:
                    return copy.deepcopy(_project(doc, projection))
                return None
        return None

    def delete_one(self, query):
        self._tally("delete_one")
        with self._lock_for(query.get("_id")):
            hits = self._scan(query)
            if hits:
                del self.docs[hits[0]["_id"]]
                return _Result(deleted=1)
        return _Result(deleted=0)

    def delete_many(self, query):
        self._tally("delete_many")
        hits = self._scan(query or {})
        for h in hits:
            self.docs.pop(h["_id"], None)
        return _Result(deleted=len(hits))

    def bulk_write(self, ops, ordered=True):
        self._tally("bulk_write")
        modified, upserted = 0, {}
        for i, op in enumerate(ops):
            filt = getattr(op, "_filter", None)
            if filt is None:
                raise UnknownOperator(f"bulk_write 연산 미구현: {type(op).__name__}")
            if getattr(op, "_doc", None) is not None:      # ReplaceOne
                r = self.replace_one(filt, op._doc, upsert=op._upsert)
            else:                                          # UpdateOne
                r = self.update_one(filt, op._update, upsert=op._upsert)
            modified += r.modified_count
            if r.upserted_id is not None:
                upserted[i] = r.upserted_id
        return _Result(modified=modified, upserted_ids=upserted)


def install(module, mapping):
    """모듈의 `*_col` 전역을 가짜 컬렉션으로 갈아끼웁니다."""
    made = {}
    for attr in mapping:
        col = FakeCollection(attr)
        setattr(module, attr, col)
        made[attr] = col
    return made

class FakeDatabase:
    """`mongo_db[name]` · `list_collection_names()` 를 흉내 냅니다.

    `export_full_payload()` 는 컬렉션 목록을 **DB 에서 직접** 읽습니다
    (그래야 포인트 시스템과 인증 시스템이 한 파일에 다 담깁니다).
    그래서 개별 `*_col` 만 갈아끼우면 백업이 텅 비어 나옵니다.
    """

    def __init__(self, mapping=None):
        self._cols = dict(mapping or {})

    def add(self, name, col):
        self._cols[name] = col
        return col

    def list_collection_names(self):
        return [n for n, c in self._cols.items() if c.docs]

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = FakeCollection(name)
        return self._cols[name]

