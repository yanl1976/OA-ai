#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""存量云之家文档补写业务流水号 doc_no。

背景：早期版本只在落盘时记了 formInstId（用于去重），未把云之家流水号
serialNo（形如 HYJYXSSPFB-20260901-002）写进文档元数据；而 serialNo 内含
「单据日期 + 当日序号」，是会议纪要等单据列表排序最权威的依据。

本脚本为存量文档补齐 doc_no，使排序无需依赖「从文件名猜日期」：
  1) 调云之家流程列表接口（find_flows），一次拿到全部单据的 serialNo +
     formInstId（只翻列表，不下载附件，速度快）；
  2) 读取去重记录 .yzj_pull_synced.json 中 formInstId -> doc_ids 的映射；
  3) 按映射把 serialNo 回填到对应文档的 doc_no 字段。

用法（默认只预览，不改动；加 --apply 才实际写入）：
    python3 scripts/backfill_doc_no.py              # 预览
    python3 scripts/backfill_doc_no.py --apply      # 实际写入
    python3 scripts/backfill_doc_no.py --task <任务id>   # 只处理某个拉取任务

提示：生产机路径为 /opt/OA-ai，脚本会自动定位项目根。
"""
import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "app"))

import kb_store  # noqa: E402
import yzj_pull  # noqa: E402


def load_tasks():
    tasks = yzj_pull.load_tasks() or []
    return [t for t in tasks if t.get("enabled", True)]


def collect_flows(task):
    """拉取该任务下的全部流程项（含 serialNo / formInstId），只翻列表不下附件。"""
    tpl_names = task.get("template_name") or []
    if isinstance(tpl_names, str):
        tpl_names = [tpl_names]
    form_code_ids = []
    raw_ids = task.get("form_code_id")
    if isinstance(raw_ids, (list, tuple)):
        form_code_ids = [str(x) for x in raw_ids if x]
    elif raw_ids:
        form_code_ids = [str(raw_ids)]
    if not form_code_ids and tpl_names:
        try:
            from yunzhijia_client import get_templates
            for t in get_templates() or []:
                cid = t.get("formCodeId")
                if cid and any(n in (t.get("title") or "") for n in tpl_names):
                    form_code_ids.append(cid)
        except Exception as e:  # noqa: BLE001
            print("[warn] 解析模板 formCodeId 失败: %s", e)
    if not form_code_ids:
        print("[warn] 任务 %s 未配置模板，跳过" % task.get("id"))
        return []
    flows = []
    for cid in form_code_ids:
        page, page_size = 1, 50
        while True:
            try:
                resp = yzj_pull.find_flows(
                    form_code_ids=[cid],
                    status=task.get("status") or None,
                    create_time=None,
                    page_number=page,
                    page_size=page_size,
                )
            except Exception as e:  # noqa: BLE001
                print("[warn] 拉取流程列表失败(cid=%s, page=%s): %s" % (cid, page, e))
                break
            batch = (resp or {}).get("list") or [] if isinstance(resp, dict) else (resp or [])
            flows.extend(batch)
            if len(batch) < page_size:
                break
            total = (resp or {}).get("total") if isinstance(resp, dict) else None
            try:
                total = int(total) if total is not None else None
            except (TypeError, ValueError):
                total = None
            if total is not None and len(batch) * page >= total:
                break
            page += 1
            if page > 500:  # 安全阀，避免异常分页死循环
                break
    return flows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    ap.add_argument("--task", default="", help="只处理指定任务 id")
    ap.add_argument("--synced", default="", help="去重记录路径（默认自动定位）")
    args = ap.parse_args()

    # formInstId -> serialNo
    serial_by_inst = {}
    tasks = load_tasks()
    if args.task:
        tasks = [t for t in tasks if t.get("id") == args.task or t.get("name") == args.task]
    if not tasks:
        print("没有可处理的拉取任务")
        return
    for t in tasks:
        flows = collect_flows(t)
        print("任务 %s(%s): 拉到 %d 条流程项" % (t.get("id"), t.get("name"), len(flows)))
        for f in flows:
            inst = f.get("formInstId") or f.get("flowInstId")
            serial = (f.get("serialNo") or "").strip()
            if inst and serial:
                serial_by_inst[inst] = serial
    print("可解析流水号的单据数: %d" % len(serial_by_inst))
    if not serial_by_inst:
        return

    # 去重记录：formInstId -> doc_ids
    synced_path = args.synced or getattr(yzj_pull, "SYNCED_FILE", "")
    if not synced_path or not os.path.exists(synced_path):
        cand = os.path.join(_ROOT, "config", ".yzj_pull_synced.json")
        synced_path = cand if os.path.exists(cand) else synced_path
    if not synced_path or not os.path.exists(synced_path):
        print("未找到去重记录 .yzj_pull_synced.json，无法建立 doc_id 映射")
        return
    synced = json.load(open(synced_path, encoding="utf-8"))
    print("去重记录条数: %d" % len(synced))

    # 待回填：doc_id -> serialNo（只填当前缺 doc_no 的）
    ups = kb_store._load_uploads()
    by_id = {u.get("doc_id"): u for u in ups if isinstance(u, dict)}
    todo = {}
    for inst, serial in serial_by_inst.items():
        rec = synced.get(inst) or {}
        for did in (rec.get("doc_ids") or []):
            u = by_id.get(did)
            if not u:
                continue
            if (u.get("doc_no") or "").strip():
                continue  # 已有流水号，不覆盖
            todo[did] = serial

    print("待补写 doc_no 的文档数: %d" % len(todo))
    if not todo:
        print("（无需补写）")
        return

    if not args.apply:
        print("\n=== 预览（前 30 条）===")
        for i, (did, serial) in enumerate(list(todo.items())[:30]):
            print("  %s  <-  %s" % (serial, (by_id[did].get("filename") or "")[:60]))
        print("\n这是预览，未做任何修改。加 --apply 实际写入。")
        return

    # 实际写入
    with kb_store._STORE_LOCK:
        ups = kb_store._load_uploads()
        n = 0
        for u in ups:
            if not isinstance(u, dict):
                continue
            did = u.get("doc_id")
            if did in todo:
                u["doc_no"] = todo[did]
                n += 1
        kb_store._atomic_write_json(kb_store.UPLOAD_FILE, ups)
    print("已补写 doc_no: %d 条" % n)


if __name__ == "__main__":
    main()
