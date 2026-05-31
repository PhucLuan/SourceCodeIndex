# Graph-Aware Search Roadmap

## Muc tieu
Nang cap app hien tai tu "semantic code search + RAG" sang "graph-aware code search" de co the tra loi cac cau hoi nhu:

- File A import file nao?
- Function C goi function D o dau?
- Cau truc phu thuoc cua mot feature la gi?
- Neu sua file nay thi anh huong toi nhung node nao?
- Nhom nay thuoc layer nao va luong business di qua cac node nao?

Muc tieu truoc mat la cai thien search va response generation. Khong can UI ve graph ngay lap tuc. Graph co the duoc tra ve trong response duoi dang adjacency list hoac Mermaid diagram do LLM tao ra.

## Hien trang va gap

### Hien trang hien co
- `ast_chunker.py` da parse AST cho Python, C#, JavaScript, TypeScript, TSX, HTML, CSS.
- `indexer_flow.py` da luu metadata tot cho node: `node_type`, `node_name`, `puid`, `parent_puid`, `is_skeleton`, `repo_name`.
- `rag.py` da co query expansion, hybrid search, rerank, context enrichment qua `parent_puid`.
- Search hien tai tot cho "tim doan code lien quan", nhung chua tot cho "tim quan he giua cac node".

### Gap can dong
- Chua co edge table de luu `imports`, `calls`, `inherits`, `contains`, `exports`, `depends_on`.
- Chua co symbol resolver de lien ket `target_symbol` sang node cu the.
- Chua co explicit file node va namespace/kieu dinh danh on dinh cho graph.
- `AstChunk.references` dang chua duoc dung de sinh relation.
- Context enrichment hien tai chi dung `parent_puid`, chua the mo rong multi-hop graph.
- Chua co search intent classifier de phan biet "semantic search" vs "graph traversal" vs "impact analysis".

## Nguyen tac thiet ke

1. Giu pipeline hien tai, mo rong thay vi rewrite toan bo.
2. Tach ro 3 lop:
   - Structural extraction
   - Relationship resolution
   - Retrieval orchestration
3. Graph phai query duoc bang SQL truoc, LLM chi dung de tong hop va dien giai.
4. Moi task phai co:
   - Output ro rang
   - Test query
   - Expected result
5. Neu chua giai quyet duoc symbol 100%, phai luu trang thai `resolved`, `ambiguous`, `external`, khong duoc "doan" im lang.

## Target architecture

### Data model muc tieu
- `graph_nodes`
  - file, function, class, method, module, concept
- `graph_edges`
  - source, target, edge_type, confidence, resolution_status, metadata
- `graph_symbols`
  - symbol index de resolver
- `graph_layers`
  - API, Service, Data, UI, Utility, Domain
- `graph_tours`
  - guided walkthrough steps theo dependency order

### Retrieval flow muc tieu
1. User query
2. Query intent detection
3. Seed retrieval:
   - semantic
   - BM25
   - fuzzy symbol lookup
4. Graph expansion:
   - 1 hop
   - 2 hop khi can
5. Evidence ranking:
   - edge type
   - distance
   - confidence
   - file importance
6. LLM response:
   - plain English
   - citations
   - optional Mermaid graph

---

## Phase 0 - Baseline, inventory, and test harness

### Muc tieu phase
Chot trang thai hien tai thanh baseline de sau nay so sanh duoc chat luong tung task.

### Task 0.1 - Inventory codebase capability
**Lam gi**
- Liet ke tat ca ngon ngu, file source, schema dang co, query mode dang co.
- Xac dinh nhung file trung tam: `app.py`, `indexer_flow.py`, `rag.py`, `ast_chunker.py`.

**Expected outcome**
- Co mot bang tong ket:
  - language support
  - node metadata
  - current search modes
  - current limitation

**Test**
- Query: "app nay dang search duoc gi?"
- Expected result:
  - Tra ve semantic search, hybrid search, reranker neu bat
  - Khong bao da co call graph hoan chinh

### Task 0.2 - Create golden query set
**Lam gi**
- Tao bo cau hoi mau theo cach user thuong hoi.
- Moi cau co `intent`, `expected evidence`, `expected failure mode`.

**Expected outcome**
- Co bo query dung lam regression test cho toan bo roadmap.

**Golden queries**
| ID | Query | Intent | Expected result |
| --- | --- | --- | --- |
| Q01 | "File nao import module nay?" | imports | Tra ve danh sach file co edge `imports` nguon den module do |
| Q02 | "Function A goi function nao?" | calls | Tra ve cac callee direct, co citation line |
| Q03 | "Class nay co nhung method nao?" | contains | Tra ve class node + child methods |
| Q04 | "Diem bat dau cua feature login la gi?" | entrypoint | Tra ve file entrypoint, route/controller, service chain |
| Q05 | "Neu sua ham validate thi anh huong toi dau?" | impact | Tra ve reverse dependency tree |
| Q06 | "Module nao xu ly auth?" | semantic | Tra ve top relevant nodes, sau do graph expansion |
| Q07 | "Cho toi luong xu ly request tu UI toi API" | business flow | Tra ve chain UI -> controller -> service -> data |
| Q08 | "Co function nao ten gan giong AddAsync khong?" | fuzzy symbol | Tra ve exact symbol + near matches |
| Q09 | "File nao test function nay?" | tested_by | Tra ve test files neu edge duoc sinh |
| Q10 | "Ve cho toi graph cua call chain nay" | graph output | Tra ve Mermaid hoac adjacency list |

### Task 0.3 - Define evaluation rubric
**Lam gi**
- Dinh nghia thang cham cho moi query:
  - relevance
  - precision
  - coverage
  - citation quality

**Expected outcome**
- Co rule de ket luan task pass/fail.

**Pass criteria**
- Top-1 phai dung voi exact symbol query.
- Graph query phai tra ve canh dung thay vi chi tra ve doan code lien quan.
- Neu khong du thong tin, LLM phai noi "chua du context" thay vi doan.

---

## Phase 1 - Normalize node identity and structural schema

### Muc tieu phase
Bien moi file/function/class thanh mot node graph co dinh danh on dinh va truy vet duoc.

### Task 1.1 - Normalize PUID and file nodes
**Lam gi**
- Chuan hoa `puid` theo format on dinh:
  - `repo_name::relative_path::kind::qualified_name`
- Tao explicit file node cho moi file.
- Tach skeleton node khong lam sai parent lookup.

**Expected outcome**
- Moi file co it nhat 1 file node.
- Moi function/class co node id on dinh.
- `parent_puid` tro duoc ve parent that su.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Mo ta file nay" | Tra ve file node + summary |
| "Class nao chua method nay?" | Tra ve parent class node chinh xac |

### Task 1.2 - Expand node metadata
**Lam gi**
- Bo sung metadata:
  - `qualified_name`
  - `signature`
  - `docstring`
  - `modifiers`
  - `export_status`
  - `source_span`

**Expected outcome**
- Search co the rank duoc theo ten chinh xac, khong chi theo doan text.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Tim method co ten verify" | Tra ve node method verify, khong lan voi text khac |
| "Tim class export ra ngoai" | Tra ve class co export status |

### Task 1.3 - Define graph node contract
**Lam gi**
- Chot schema cho node:
  - file
  - function
  - class
  - method
  - module
  - concept

**Expected outcome**
- Toan bo subsequent task dung cung 1 contract, khong sinh ra node loang lo.

**Test**
- Query: "Node loai nao hien co trong graph?"
- Expected result:
  - file/function/class/method/module/concept

---

## Phase 2 - Deterministic relationship extraction

### Muc tieu phase
Sinh edge co the dung de tra loi cau hoi quan he code ma khong can LLM doan.

### Task 2.1 - Extract imports and exports
**Lam gi**
- Parse `import`, `from import`, `export`, `using`, `require`, `module.exports`.
- Luu edge:
  - `imports`
  - `exports`

**Expected outcome**
- Co the tra loi:
  - file nao import file nao
  - symbol nao duoc export

**Test queries**
| Query | Expected result |
| --- | --- |
| "File nao import cai nay?" | Tra ve list source files co edge `imports` |
| "Function nay duoc export khong?" | Tra ve edge `exports` neu co |

### Task 2.2 - Extract calls and constructor usage
**Lam gi**
- Parse `call_expression`, `invocation_expression`, `new_expression`, `method invocation`.
- Luu edge `calls`.
- Ghi `target_symbol` neu chua resolve duoc.

**Expected outcome**
- Co call graph thuc te, it nhat o muc intra-file va best-effort inter-file.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Function C goi function nao?" | Tra ve cac callee direct |
| "Luong call cua ham nay la gi?" | Tra ve danh sach edges calls theo thu tu hop ly |

### Task 2.3 - Extract inheritance and implementation
**Lam gi**
- Parse `extends`, `implements`, base classes, interface implementations.
- Luu edge:
  - `inherits`
  - `implements`

**Expected outcome**
- Co the tra loi "class nay ke thua gi?".

**Test queries**
| Query | Expected result |
| --- | --- |
| "Class nay ke thua tu dau?" | Tra ve parent class |
| "Interface nao duoc implement?" | Tra ve danh sach edge `implements` |

### Task 2.4 - Capture containment and nesting
**Lam gi**
- Luu `contains` giua file -> class/function, class -> method.
- Giữ line range va nesting level.

**Expected outcome**
- Co parent-child tree ro rang, phuc vu both search va guided tours.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Class nay co method nao?" | Tra ve child methods |
| "File nay co nhung node nao?" | Tra ve toan bo child nodes |

### Task 2.5 - Store unresolved and ambiguous edges
**Lam gi**
- Khong ep resolve ngay.
- Luu trang thai:
  - `resolved`
  - `ambiguous`
  - `external`
  - `unresolved`

**Expected outcome**
- He thong khong mat edge ngay ca khi chua resolve duoc symbol.

**Test**
- Query: "import tu package ngoai co thay khong?"
- Expected result:
  - Tra ve `external` edge hoac note that symbol la external

---

## Phase 3 - Symbol index and linker

### Muc tieu phase
Bien edge thuc te thanh graph co the di chuyen qua lai giua cac node.

### Task 3.1 - Build symbol index
**Lam gi**
- Tao index theo:
  - symbol name
  - qualified name
  - file path
  - export status
  - language

**Expected outcome**
- Tim symbol nhanh va co tiep can fuzzy.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Tim AddAsync" | Tra ve node dung va cac alias gan dung |
| "Tim validateCredentials" | Tra ve symbol theo ten chinh xac |

### Task 3.2 - Resolve imports to files
**Lam gi**
- Map `target_symbol` cua import sang file node tuong ung.
- Dung relative import, same repo namespace, export map.

**Expected outcome**
- File A import file B co the di duoc bang SQL join.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Module nay import file nao?" | Tra ve danh sach file target da resolve |
| "File nao duoc import nhieu nhat?" | Tra ve top nodes theo inbound imports |

### Task 3.3 - Resolve calls to callee nodes
**Lam gi**
- Dung symbol index + scope + import context de map call sang node target.
- Neu khong chac, luu multi-candidate.

**Expected outcome**
- Query call graph khong con chi la text match.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Ham nay goi ham nao trong file khac?" | Tra ve callee nodes da resolve |
| "Ai goi validateCredentials?" | Tra ve reverse call edges neu co |

### Task 3.4 - Add graph query helpers
**Lam gi**
- Tao helper API:
  - get_neighbors
  - get_incoming_edges
  - get_outgoing_edges
  - get_shortest_path

**Expected outcome**
- RAG co the expand graph theo hop.

**Test**
- Query: "Cho toi neighbors cua node nay"
- Expected result:
  - tra ve node lien quan theo edge type

---

## Phase 4 - Graph-aware retrieval

### Muc tieu phase
Upgrade `rag.py` tu "retrieve chunks" thanh "retrieve graph evidence".

### Task 4.1 - Add query intent detection
**Lam gi**
- Phan loai query thanh:
  - semantic
  - symbol lookup
  - dependency
  - call flow
  - impact analysis
  - architecture tour
  - domain/business flow

**Expected outcome**
- Moi query di vao pipeline phu hop.

**Test queries**
| Query | Expected intent |
| --- | --- |
| "Ham AddAsync o dau?" | symbol lookup |
| "Ham nay goi gi?" | call flow |
| "Sua file nay anh huong gi?" | impact analysis |
| "Luong xu ly login" | domain/business flow |

### Task 4.2 - Seed retrieval with graph context
**Lam gi**
- Dung semantic + BM25 de tim seed nodes.
- Sau do expand 1 hop theo edge types co trong intent.

**Expected outcome**
- Query "file A import file B" khong can dua vao embedding alone.

**Test queries**
| Query | Expected result |
| --- | --- |
| "File nao import service nay?" | Tra ve file seed + imported targets |
| "Function C goi D o dau?" | Tra ve caller + callee + line evidence |

### Task 4.3 - Add graph evidence block to LLM prompt
**Lam gi**
- Bo sung prompt context:
  - seed nodes
  - edges
  - hop distance
  - confidence
  - unresolved notes

**Expected outcome**
- LLM tra loi co can cu graph, khong chi doc chunk text.

**Test**
- Query: "Ve cho toi graph nay"
- Expected result:
  - LLM tra ve adjacency list hoac Mermaid

### Task 4.4 - Make LLM able to emit graph snippets
**Lam gi**
- Cho prompt yeu cau LLM in:
  - short explanation
  - citation list
  - Mermaid graph neu phu hop

**Expected outcome**
- Khong can UI graph, van co the "ve graph" trong response.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Ve luong call cua login" | Tra ve Mermaid sequence hoac flow graph |
| "Ve import graph cua module nay" | Tra ve Mermaid flow hoac edge list |

---

## Phase 5 - User-facing search scenarios

### Muc tieu phase
Dam bao app tra loi duoc cac cau hoi user hay dung trong thuc te.

### Task 5.1 - Exact symbol search
**Lam gi**
- Tai uu cho ten ham/class/module chinh xac.
- Dung fuzzy fallback khi can.

**Expected outcome**
- Query ten ham/class phai ra dung node top-1.

**Scenario tests**
| Query | Expected result |
| --- | --- |
| "AddAsync" | Node AddAsync top-1 |
| "validateCredentials" | Node validateCredentials top-1 |
| "AuthService" | Class AuthService top-1 |

### Task 5.2 - Dependency search
**Lam gi**
- Tra loi "file nao import file nao", "module nao phu thuoc module nao".

**Expected outcome**
- Tra ve danh sach edge imports, khong chi chunk text.

**Scenario tests**
| Query | Expected result |
| --- | --- |
| "File nao import db connection?" | Tra ve file co import do |
| "Module nao phu thuoc vao auth?" | Tra ve inbound dependency nodes |

### Task 5.3 - Call flow search
**Lam gi**
- Tra loi "function C goi function D", "luong xu ly tu A sang B".

**Expected outcome**
- Tra ve caller, callee, line numbers, va path neu co nhieu hop.

**Scenario tests**
| Query | Expected result |
| --- | --- |
| "Ham login goi nhung ham nao?" | Tra ve danh sach callee |
| "Ai goi validateCredentials?" | Tra ve callers |

### Task 5.4 - Impact analysis search
**Lam gi**
- Reverse traversal tu node/edge target.
- Tra ve "affected by change".

**Expected outcome**
- Co bang anh huong theo 1 hop va 2 hop.

**Scenario tests**
| Query | Expected result |
| --- | --- |
| "Neu doi ham nay thi anh huong file nao?" | Tra ve reverse graph list |
| "Neu sua interface nay thi ai bi anh huong?" | Tra ve implementers/callers/importers |

### Task 5.5 - Guided tour generation
**Lam gi**
- Topological sort theo dependency.
- LLM tao walkthrough theo thu tu doc code.

**Expected outcome**
- Co "tour" theo architecture, entrypoint -> service -> data.

**Scenario tests**
| Query | Expected result |
| --- | --- |
| "Dan toi cach hieu codebase nay" | Tra ve tour theo thu tu dependency |
| "Bat dau tu dau de doc feature login?" | Tra ve guided tour cua login flow |

### Task 5.6 - Layer and domain view
**Lam gi**
- Heuristic layering dau tien:
  - API
  - Service
  - Data
  - UI
  - Utility
- LLM refine neu can.

**Expected outcome**
- Co the group node theo layer va domain.

**Scenario tests**
| Query | Expected result |
| --- | --- |
| "Nhung file nao thuoc API layer?" | Tra ve API nodes |
| "Business flow cua login gom nhung buoc nao?" | Tra ve domain steps |

---

## Phase 6 - Quality, regression, and hardening

### Muc tieu phase
Dam bao enhancements khong lam sua search hien tai va co regression suite ro rang.

### Task 6.1 - Build regression suite
**Lam gi**
- Chuan hoa bo 15-20 queries thanh test set co expected evidence.
- Chay sau moi thay doi extractor/resolver/retrieval.

**Expected outcome**
- Co checklist pass/fail cho tung task.

**Regression groups**
| Group | So query | Muc tieu |
| --- | --- | --- |
| Exact symbols | 5 | Do chinh xac ten node |
| Dependency | 5 | Do chinh xac graph edge |
| Call flow | 5 | Do chinh xac traversal |
| Impact | 3 | Do reverse traversal |
| Guided tour | 2 | Do ket cau architecture |

### Task 6.2 - Add ambiguity handling tests
**Lam gi**
- Test case co 2 function cung ten.
- Test case external package.
- Test case unresolved import.

**Expected outcome**
- He thong khong tra loi sai confidence.

**Test queries**
| Query | Expected result |
| --- | --- |
| "Tim helper nay trong 2 file cung ten" | Tra ve nhieu candidate va giai thich ambiguous |
| "Import tu package ngoai o dau?" | Tra ve external note |

### Task 6.3 - Add performance guardrails
**Lam gi**
- Do latency cho:
  - seed retrieval
  - graph expansion
  - prompt build
  - LLM answer

**Expected outcome**
- Co nguong canh bao neu traversal qua cham.

**Acceptance targets**
| Step | Target |
| --- | --- |
| Semantic seed retrieval | < 500 ms typical local |
| Graph expansion 1 hop | < 300 ms typical |
| Prompt build | < 100 ms |
| Full answer | phu thuoc LLM, nhung phai co logging ro rang |

---

## Acceptance checklist theo phase

### Phase 0 pass khi
- Co baseline doc.
- Co golden query set.
- Co rubric cham diem.

### Phase 1 pass khi
- Moi file/function/class co dinh danh on dinh.
- Parent-child lookup khong con lech skeleton.

### Phase 2 pass khi
- Co imports/calls/inherits/contains edges.
- Co unresolved/ambiguous handling.

### Phase 3 pass khi
- Co symbol index.
- Co linker lay node target.
- Co graph helper queries.

### Phase 4 pass khi
- Query intent routing hoat dong.
- Graph-aware retrieval tra ve evidence dung.
- LLM co the emit Mermaid/adjacency list.

### Phase 5 pass khi
- Cac query thuc te cua user tra loi dung:
  - exact symbol
  - dependency
  - call flow
  - impact
  - guided tour
  - layer/domain

### Phase 6 pass khi
- Regression suite on dinh.
- Ambiguity handling va performance guardrails co test.

---

## Appendix A - Golden query pack

| ID | Query | Expected answer shape |
| --- | --- | --- |
| G01 | "AddAsync o dau?" | Exact node + file + line |
| G02 | "validateCredentials goi gi?" | Call edges + line citations |
| G03 | "File nao import db connection?" | Import edges |
| G04 | "AuthService co method nao?" | Contains edges |
| G05 | "Ai goi login handler?" | Incoming call edges |
| G06 | "Neu doi interface nay thi ai bi anh huong?" | Reverse dependency list |
| G07 | "Luong xu ly login tu UI toi DB" | Multi-hop path |
| G08 | "Tim code xu ly error khi tao asset" | Semantic seed + graph expansion |
| G09 | "Ve graph cua module nay" | Mermaid / adjacency list |
| G10 | "Huong dan doc codebase nay" | Guided tour ordered by dependency |
| G11 | "Layer nao chua logic API?" | Layer classification |
| G12 | "Module nao cung chuc nang voi module nay?" | Related/similar nodes |
| G13 | "Function nay duoc test o dau?" | tested_by edges |
| G14 | "Co import cycle khong?" | Graph cycle detection result |
| G15 | "Node nao co inbound dependency nhieu nhat?" | Centrality-ish ranking |

## Appendix B - Notes for implementation

- Khong can doi UI graph ngay.
- Co the tra graph trong response cua LLM bang text hoac Mermaid.
- Neu query thuoc graph intent, uu tien SQL graph thay vi semantic search.
- Neu query khong ro intent, chay hybrid: semantic seed + graph expansion.
- Neu current index duoc re-index, phai co migration/compatibility note cho node id cu.
