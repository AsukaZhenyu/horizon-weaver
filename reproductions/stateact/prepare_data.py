"""Prepare only official ALFWorld OOD text-game data, never policy demonstrations."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import httpx
from alfworld.info import ALFRED_PDDL_PATH, ALFRED_TWL2_PATH

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".horizon" / "reproductions" / "stateact"
DATA = CACHE / "data"
DOWNLOADS = CACHE / "downloads"
URLS = [
    "https://github.com/alfworld/alfworld/releases/download/0.2.2/json_2.1.1_json.zip",
    "https://github.com/alfworld/alfworld/releases/download/0.2.2/json_2.1.1_pddl.zip",
    "https://github.com/alfworld/alfworld/releases/download/0.4.0/json_2.1.2_tw-pddl.zip",
]


def main():
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    records = []
    with httpx.Client(follow_redirects=True, timeout=120.0, trust_env=False) as client:
        for url in URLS:
            target = DOWNLOADS / url.rsplit("/", 1)[1]
            if not target.exists():
                partial = target.with_suffix(target.suffix + ".partial")
                print("Downloading " + target.name, flush=True)
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with partial.open("wb") as stream:
                        for chunk in response.iter_bytes():
                            stream.write(chunk)
                partial.replace(target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            extracted = 0
            extracted_members = []
            with zipfile.ZipFile(target) as archive:
                for member in archive.infolist():
                    # Full OOD split; train/seen datasets and 3D weights are unnecessary.
                    if "/valid_unseen/" not in member.filename or member.is_dir():
                        continue
                    destination = (DATA / member.filename).resolve()
                    if not destination.is_relative_to(DATA.resolve()):
                        raise RuntimeError("Unexpected archive path")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        with archive.open(member) as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                    extracted += 1
                    extracted_members.append(member.filename)
            records.append({"url": url, "sha256": digest, "files_in_ood_split": extracted,
                            "extracted_members": sorted(extracted_members)})
            print(f"{target.name}: OOD files={extracted}, sha256={digest}", flush=True)
    logic = DATA / "logic"
    logic.mkdir(exist_ok=True)
    shutil.copyfile(ALFRED_PDDL_PATH, logic / "alfred.pddl")
    shutil.copyfile(ALFRED_TWL2_PATH, logic / "alfred.twl2")
    task_ids = sorted(str(path.parent.relative_to(DATA)) for path in DATA.rglob("game.tw-pddl"))
    manifest = {"archives": records, "extracted_task_ids": task_ids}
    (CACHE / "data-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Prepared official text-game OOD split.", flush=True)


if __name__ == "__main__":
    main()
