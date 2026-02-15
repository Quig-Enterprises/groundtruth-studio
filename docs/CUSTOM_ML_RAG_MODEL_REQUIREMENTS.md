# Custom ML/RAG Model Requirements

Requirements and standards for building custom ML classifiers with RAG-augmented explanations on the Artemis stack.

## 1. Stack Overview

| Layer | Technology | Purpose |
|-------|-----------|---------|
| ML Framework | scikit-learn | Classification (RandomForest, etc.) |
| Signal Processing | SciPy, NumPy | Feature extraction from raw sensor data |
| Model Serialization | `.pkl` artifacts | Persist trained models and scalers |
| RAG Vector Store | ChromaDB (SQLite-backed) | Semantic retrieval of domain knowledge |
| Embeddings | HuggingFace `all-MiniLM-L6-v2` | Chunk embedding via `langchain_huggingface` |
| RAG Orchestration | LangChain | Document loading, splitting, retrieval chains |
| Serving | Flask | HTTP API for classifier + RAG retrieval |
| LLM Inference | Ollama (local) | Natural language explanation generation |
| Runtime | Python 3.x with venv | Isolated per-model environments |

## 2. Directory Structure

Each custom model lives under `/models/custom/<model-name>/` and must follow this layout:

```
/models/custom/<model-name>/
  ml_classifier.py              # Training + prediction code
  <model-name>_classifier.pkl   # Trained model artifact
  <model-name>_scaler.pkl       # Feature scaler artifact
  rag_service.py                # Flask RAG retrieval API
  scripts/
    build_vectorstore.py        # Vectorstore build script (idempotent)
    download_sources.py         # Source acquisition script
  sources/
    RAG_SOURCES.md              # Catalog of all knowledge sources with URLs
    cleaned/                    # Processed markdown ready for chunking
      papers/
      standards/
      textbooks/
    datasets/                   # Raw training/evaluation data
  vectorstore/                  # ChromaDB persistent store (rebuild, don't commit)
  venv/                         # Python virtual environment (don't commit)
  TODO.md                       # Model-specific backlog
```

## 3. ML Classifier Requirements

### 3.1 Feature Extraction

- Extract features from raw sensor signals (vibration, current, temperature, etc.)
- Feature vector must include both **time-domain** and **frequency-domain** features
- Recommended minimum feature set:
  - Time: RMS, peak, crest factor, kurtosis, skewness, std deviation
  - Frequency: spectral centroid, spectral spread, band energy ratios
  - Envelope: envelope RMS, envelope kurtosis, envelope spectral energy
- Features must be documented in a `FEATURE_NAMES` list matching the extraction order

### 3.2 Training

- Use `train_test_split` with stratified sampling and fixed `random_state` for reproducibility
- Apply `StandardScaler` — persist scaler alongside model
- Report classification metrics: precision, recall, F1 per class, confusion matrix
- Run cross-validation (minimum 5-fold) and report mean +/- std
- Log top feature importances

### 3.3 Model Artifacts

- Classifier saved as `<model-name>_classifier.pkl`
- Scaler saved as `<model-name>_scaler.pkl`
- Version artifacts with suffix (e.g. `_v3.pkl`, `_v4.pkl`) — keep previous versions
- Include a `predict()` function that accepts raw data + sampling frequency and returns:
  ```python
  {
      "diagnosis": str,        # Human-readable label
      "confidence": float,     # 0.0 - 1.0
      "probabilities": dict    # Label -> probability for all classes
  }
  ```

### 3.4 Serving

- Flask service with `/predict`, `/health` endpoints
- Managed by systemd (unit file in `/etc/systemd/system/`)
- Load model and scaler once at startup, not per-request

## 4. RAG Pipeline Requirements

### 4.1 Source Management

- Maintain `sources/RAG_SOURCES.md` cataloging every source with URL, category, and focus area
- Source categories: `textbook`, `standard`, `paper`, `oem_guide`, `dataset`, `signal_processing`
- **All sources must be converted to markdown before chunking.** PDFs, DOCX, HTML, and other formats go through a cleaning pipeline:
  1. Download raw file to `sources/<category>/` (original preserved)
  2. Convert to markdown (e.g. PDF -> text extraction -> manual cleanup for tables/figures)
  3. Store cleaned markdown in `sources/cleaned/<category>/<source_name>.md`
  4. Only files in `sources/cleaned/` are fed to the chunker — never raw PDFs
- `download_sources.py` must be idempotent and handle failures gracefully

### 4.2 Chunking Rules

#### Base Settings

| Setting | Value | Rationale |
|---------|-------|-----------|
| Splitter | `RecursiveCharacterTextSplitter` | Markdown-aware recursive splitting |
| Default chunk size | 1500 characters | Balances context completeness vs retrieval precision |
| Chunk overlap | 200 characters | Prevents information loss at boundaries |
| Separators | `["\n## ", "\n### ", "\n#### ", "\n\n", "\n", " "]` | Respects markdown heading hierarchy |

#### Variable Chunk Sizes by Content Type

Not all content benefits from the same chunk size. Apply content-aware sizing:

| Source Type | Chunk Size | Rationale |
|-------------|-----------|-----------|
| `standard` | ~1000 chars | Standards contain tables, criteria, and thresholds — smaller chunks improve retrieval precision |
| `textbook` | ~2000 chars | Explanatory content needs more surrounding context to be useful |
| `paper` | Section-based | Split by abstract, methods, results, discussion — each section is a natural unit |
| `oem_guide` | 1500 chars (default) | Mixed content, default works well |
| `signal_processing` | 1500 chars (default) | Technical methods, default works well |

The `source_type` metadata field (see 4.4) determines which chunk size to apply.

### 4.3 Pre-Processing (before chunking)

These steps run on the cleaned markdown **before** it reaches the splitter:

#### 1. Table Protection

Detect markdown tables and treat them as atomic blocks — never split mid-table.

- If a table fits within `chunk_size`, keep it as a single indivisible unit
- If a table exceeds `chunk_size`, it becomes its own chunk (oversized is acceptable — better than a split table)
- Tag chunks containing tables with `has_table: True` in metadata

#### 2. Header Context Injection

Prepend the full heading hierarchy to every chunk so section context survives splitting:

```
[Source: ABS_Ship_Vibration.md > ## Section 4 > ### 4.3 Acceptance Criteria]

The acceptable vibration velocity for...
```

Implementation: walk the markdown AST, track the active header stack, and prepend the chain as a bracketed prefix before the content enters the splitter.

#### 3. Orphan Recovery (second splitter pass)

After splitting, check each chunk for header context. If a chunk has no header prefix (orphaned paragraph from a split), walk back up the original markdown to find its nearest parent header and attach it.

This guarantees **every chunk has a header chain** — no chunk is ever context-free.

#### 4. Equation Detection

Scan each chunk for LaTeX-style equations (`$...$`, `$$...$$`, `\begin{equation}`). Tag with `has_equation: True` in metadata. Do not split inside equation blocks.

### 4.4 ChromaDB Metadata

Every chunk stored in ChromaDB must carry the following metadata fields:

| Field | Type | Description | Enables |
|-------|------|-------------|---------|
| `source` | `str` | Full file path of the source document | Basic citation |
| `source_name` | `str` | Filename without path (e.g. `NSK_bearing_doctor.md`) | Display-friendly citation |
| `source_type` | `str` | Category: `paper`, `standard`, `textbook`, `oem_guide`, `dataset`, `signal_processing` | Filtered retrieval by category |
| `header_chain` | `str` | Full heading hierarchy (e.g. `Diagnosis > Inner Race > Envelope`) | Section-level citations in responses |
| `section_headers` | `list[str]` | List of parent headers in order (e.g. `["Section 4", "4.3 Acceptance Criteria"]`) | Structured section navigation |
| `section_header` | `str` | Immediate parent heading | Scoped retrieval within a topic |
| `chunk_index` | `int` | Position of this chunk within its source document (0-based) | Ordering, deduplication, sequential context reconstruction |
| `has_table` | `bool` | Whether this chunk contains a markdown table | Filter for criteria/threshold lookups |
| `has_equation` | `bool` | Whether this chunk contains equations | Filter for formula-heavy content |

**Usage examples enabled by metadata:**
- Filter retrieval: `where={"source_type": "standard"}` — only search ISO/ABS standards
- Table-specific search: `where={"has_table": True}` — find acceptance criteria tables
- Citation in response: "per *Section 4.3 Acceptance Criteria* of ABS Ship Vibration"
- Reconstruct context: fetch adjacent chunks by `source` + `chunk_index` range

### 4.5 Vectorstore Build

- `scripts/build_vectorstore.py` must be **idempotent** — delete and rebuild from scratch
- Verify chunk count after persist matches expected count
- Run a smoke-test query and print sample results
- Log chunk distribution per source document

### 4.6 RAG Retrieval Service

- Flask service on dedicated port (e.g. 5004)
- Endpoints: `/retrieve?query=TEXT&k=6`, `/health`, `/sources`
- Clamp `k` between 1 and 20
- Return L2 distance converted to similarity score (0-1 range)
- Load vectorstore once at startup
- Return `source`, `score`, and `content` for each retrieved chunk

## 5. Integration with Eqmon

- Classifier service registered in systemd and auto-started on boot
- Eqmon PHP API calls classifier service over HTTP (localhost)
- RAG retrieval integrated into Ollama prompt chains for:
  - `ai_chat.php` — conversational analysis
  - `ai_deep_analysis.php` — detailed diagnostic reports
- Auto-classification triggered on MQTT ingest of raw waveforms
- Results stored in eqmon database (e.g. `ai_bearing_health_summary`)

## 6. Quality Gates

Before a model version is promoted to production:

| Gate | Requirement |
|------|-------------|
| Cross-validation | Mean accuracy >= 85% (5-fold) |
| Per-class F1 | No class below 0.70 F1 |
| RAG retrieval | Smoke-test queries return relevant chunks (manual spot-check) |
| Service health | `/health` returns `healthy` with correct chunk count |
| Integration test | End-to-end: raw data -> classifier -> RAG context -> LLM explanation |
| Artifact versioning | Previous model version preserved (not overwritten) |

## 7. Current Models

| Model | Status | Classifier Port | RAG Port | Dataset |
|-------|--------|-----------------|----------|---------|
| `bearing-model` | Production (v4) | 5003 | 5004 | CWRU bearing fault |
| `motor` | Placeholder | - | - | TBD |
| `shipping` | Early (sources only) | - | - | ABS rules |

## 8. Backlog

Tracked in each model's `TODO.md`. Current priorities for `bearing-model`:

- [ ] Table atomic block pre-processing in chunker
- [ ] Parent header chain prepended to chunks
- [ ] Enriched ChromaDB metadata (`source_type`, `header_chain`, `section_header`, `chunk_index`)
- [ ] Additional training datasets (MaFaulDa, VBL-VA001, Korea Aerospace, UCI)
- [ ] Prognostics / remaining useful life estimation (NASA/IMS data)
