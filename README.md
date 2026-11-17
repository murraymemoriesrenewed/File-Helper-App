# Memories Renewed – File Helper

A small web service that NocoBase calls to manage order folders on the network drive (`Z:\`). It runs in Docker next to NocoBase. NocoBase calls it from workflows using an **HTTP request** step.

| Function | Call | What it does |
|---|---|---|
| Create | `POST /projects` | Creates `YYYY-MM-DD - Order - Last, First - Description` with `_FINAL`, `_RAW` and `_TRACKER` folders and the stub file `_FINAL\.Order - Last`. Returns the folder name. |
| Move | `POST /projects/move` | Moves an order folder from one stage folder to another. |
| Properties | `GET or POST /properties` | Returns the file count, folder count and total size, plus the same figures for each subfolder. |
| List | `GET or POST /list` | Returns the files with their sizes and last-modified dates. Add `recursive: true` to include subfolders. |
| Health | `GET /health` | Checks the share is reachable and all 13 standard folders exist. Needs no key. |

**Safety rules built in:**
- It only works inside the share.
- It never renames, moves or deletes the 13 standard folders (`_Stage 01 …` to `__Outbox - Done`).
- It never deletes anything at all.
- It refuses to create a second folder for an order number that already exists in any stage.
- It refuses to move a folder onto one that already exists.

---

## 1. Set up (Docker Desktop on Windows)

1. Unzip to `C:\Users\murray\file-helper`.
2. Find the network path behind `Z:` by running:
   ```powershell
   net use
   ```
   Look for the line with `Z:`, for example `\\NAS01\Projects`.
3. Create the settings file:
   ```powershell
   cd C:\Users\murray\file-helper
   Copy-Item .env.example .env
   notepad .env
   ```
   Fill in these values:
   - `HELPER_API_KEY`: a long random string. The comment in the file shows a one-line PowerShell command that generates one.
   - `SHARE_UNC`: the `net use` path written with forward slashes, for example `//NAS01/Projects`.
   - `SHARE_USER` and `SHARE_PASSWORD`: an account that can read and write the share.
4. Start it:
   ```powershell
   docker compose up -d --build
   ```
5. Check it. Open http://localhost:8765/health in a browser. You should see `"ok": true`. If `share_reachable` is false, the `SHARE_UNC`, user or password is wrong. Try `SMB_VERSION=2.1` if the server is old.
6. Try it out. Open http://localhost:8765/docs. Click **Authorize** (or add the `X-Api-Key` header), then use **Try it out** on any function.

## 2. Let NocoBase reach it

Find your NocoBase container's name with `docker ps`, then put it on the helper's network:

```powershell
docker network connect file-helper <nocobase-container-name>
```

From NocoBase, the helper's address is now **`http://file-helper:8000`**. To make the connection permanent, add the network to NocoBase's own `docker-compose.yml`:

```yaml
services:
  app:                         # your NocoBase service
    networks: [default, file-helper]
networks:
  file-helper:
    external: true
```

## 3. NocoBase setup

### Fields on your Projects collection

| Field | Type | Who fills it |
|---|---|---|
| order_number | Single line text | staff |
| order_date | Date (date only) | staff |
| last_name, first_name | Single line text | staff |
| description | Single line text | staff |
| stage | Single select, one option per stage folder name | staff, to request a move |
| folder_name | Single line text | the workflow |
| folder_stage | Single line text | the workflow (where the folder actually is) |
| folder_path | Single line text | the workflow |
| file_count, total_size | Number / text | the workflow |

There are two stage fields, `stage` and `folder_stage`. NocoBase doesn't tell a workflow what the previous value of a field was. Keeping the actual location in `folder_stage` lets the move workflow know which stage folder to move *from*.

### Workflow A: Create folder when a project is added

1. **Trigger:** Collection event → Projects → **After record added**.
2. **HTTP request** step:
   - Method: `POST`
   - URL: `http://file-helper:8000/projects`
   - Headers: `X-Api-Key` = your key, and `Content-Type` = `application/json`
   - Body (use the variable picker for each value):
     ```json
     {
       "order_date":   "{{trigger data → order_date}}",
       "order_number": "{{trigger data → order_number}}",
       "last_name":    "{{trigger data → last_name}}",
       "first_name":   "{{trigger data → first_name}}",
       "description":  "{{trigger data → description}}"
     }
     ```
3. **Update record** step → Projects, filtered to the triggering record's ID. Set these fields from the HTTP request step's response (`data`):
   - `folder_name` ← `data.folder_name`
   - `folder_stage` ← `data.stage`
   - `folder_path` ← `data.path`

   If your NocoBase version doesn't let you pick fields inside the response, add a **JSON query** step after the HTTP request to pull them out.

### Workflow B: Move folder when the stage changes

1. **Trigger:** Collection event → Projects → **After record updated**, with changed field `stage`.
2. **Condition** step: continue only if `stage` ≠ `folder_stage`.
3. **HTTP request** step:
   - Method: `POST`
   - URL: `http://file-helper:8000/projects/move`
   - Body:
     ```json
     {
       "folder_name": "{{trigger data → folder_name}}",
       "from_stage":  "{{trigger data → folder_stage}}",
       "to_stage":    "{{trigger data → stage}}"
     }
     ```
4. **Update record:** `folder_stage` ← `data.to_stage`, and `folder_path` ← `data.path`.

### Workflow C: Refresh file count and size

Use either a **Custom action** trigger (a button on the project page) or a **Schedule** trigger (nightly).

1. **HTTP request** step:
   - Method: `POST`
   - URL: `http://file-helper:8000/properties`
   - Body:
     ```json
     { "path": "{{folder_stage}}/{{folder_name}}" }
     ```
2. **Update record:** `file_count` ← `data.file_count`, and `total_size` ← `data.total_size`.

To list files, use `POST /list` with `{ "path": "...", "recursive": true }`. It returns `entries[]`, each with `name`, `relative_path`, `type`, `size_bytes`, `size` and `modified`.

## 4. Reference

### Inputs the helper accepts

- **Stages:** the full folder name, or the short forms `1`–`11`, `03` or `Stage 3`, plus `inbox` and `outbox`.
- **Paths** for properties and list can be written any of these ways:
  - `_Stage 02 - Digitized/2026-…`
  - `_Stage 02 - Digitized\2026-…`
  - `Z:\_Stage 02 - Digitized\2026-…`
  - `""` (empty) for the root of the share
- **Dates:** `2026-10-21`, or a NocoBase timestamp. Timestamps are converted to Chicago time before the date is taken.
- **Names:**
  - `/`, `\` and `|` become ` - `.
  - `< > : " ? *` are removed, because Windows doesn't allow them.
  - Extra spaces are collapsed.
  - The folder name is limited to 150 characters by shortening the description.
- **Move:** give `folder_name`, or give `order_number` and the helper finds the folder in `from_stage`.

### Errors

Errors come back as `{"detail": "message"}` with one of these codes:

| Code | Meaning |
|---|---|
| 400 | Bad input |
| 401 | Wrong or missing key |
| 404 | Folder not found |
| 409 | Already exists, or a duplicate order |
| 423 | A file in the folder is open on someone's PC, so it can't move yet |
| 503 | A stage folder is missing on the share |

Calling create twice for the same order is safe: the second call returns `"created": false`.

### Logs

```powershell
docker compose logs -f file-helper
```

Every create and move is logged.

### Tests

```powershell
pip install -r requirements.txt pytest httpx
pytest -q
```

### Running without Docker (for testing)

Run `run_local.bat`. It serves the helper on http://localhost:8765 using `Z:\` directly. Mapped drives only work this way while you're logged in, so use Docker for everyday use.
