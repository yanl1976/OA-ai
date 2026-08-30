"""云之家(金蝶)开放平台客户端：鉴权 + 会议纪要抓取。

鉴权流程（信鸿 OAuth2.0，app 级）：
  POST https://www.yunzhijia.com/gateway/oauth2/token/getAccessToken
  参数: appId, secret(=appSecret), ecpId, timestamp(毫秒)
  -> data.accessToken(7200s) + data.refreshToken

智能审批(cloudflow)业务接口（拉流程/读表单，app 级 token 即可）：
  网关前缀 https://www.yunzhijia.com/gateway/workflow/form/thirdpart/
  - getTemplates          获取工作圈全部审批模板(formCodeId/title)
  - findFlows             按模板/状态拉流程实例列表
  - viewFormInst          按 formInstId+formCodeId 读表单全部字段(含纪要正文)

配置从 .env 读取。

.env 字段：
  YUNZHIJIA_APP_ID
  YUNZHIJIA_APP_SECRET   # 对应接口 secret 字段
  YUNZHIJIA_ECP_ID       # 企业ID
  YUNZHIJIA_API_BASE     # 默认 https://www.yunzhijia.com
"""
import os
import time
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _cfg(key, default=""):
    # 去尾注：.env 里可能写成  VALUE   # 注释，需 strip
    v = os.environ.get(key, default)
    if v:
        v = v.split("#")[0].strip()
    return v


APP_ID = _cfg("YUNZHIJIA_APP_ID")
APP_SECRET = _cfg("YUNZHIJIA_APP_SECRET")
ECP_ID = _cfg("YUNZHIJIA_ECP_ID")
EID = _cfg("YUNZHIJIA_EID", ECP_ID)  # 审批 team 级需 eid，缺省复用 ECP_ID
RESGROUP_SECRET = _cfg("YUNZHIJIA_RESGROUP_SECRET")  # 文件服务 resGroupSecret 级密钥
API_BASE = _cfg("YUNZHIJIA_API_BASE", "https://www.yunzhijia.com").rstrip("/")

_HEADERS_JSON = {"Content-Type": "application/json"}

# token 缓存：按 scope 分别缓存（app 默认 / team 审批用）
_TOKENS = {}  # scope -> (token, expire_ts, refresh)


def get_access_token(scope="team", force=False):
    """获取/复用 accessToken。

    scope='team' 用于智能审批(cloudflow)等 team 级接口，需 eid；
    scope='app'  为普通轻应用级（旧默认）。
    默认 team，因为本客户端当前目标即抓取审批流纪要。
    """
    global _TOKENS
    cached = _TOKENS.get(scope)
    if not force and cached and time.time() < cached[1] - 60:
        return cached[0]
    url = f"{API_BASE}/gateway/oauth2/token/getAccessToken"
    body = {
        "appId": APP_ID,
        "secret": APP_SECRET,
        "eid": EID,
        "timestamp": int(time.time() * 1000),
        "scope": scope,
    }
    r = requests.post(url, json=body, headers=_HEADERS_JSON, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError("获取 accessToken(scope=%s) 失败：%s" % (scope, r.text[:400]))
    d = data.get("data", {})
    tok = d.get("accessToken")
    expire = int(d.get("expireIn", 7200))
    if not tok:
        raise RuntimeError("获取 accessToken(scope=%s) 失败：%s" % (scope, r.text[:400]))
    _TOKENS[scope] = (tok, time.time() + expire, d.get("refreshToken"))
    return tok


def _auth_header():
    return {"Content-Type": "application/json", "accessToken": get_access_token("team")}


# ---------------------------------------------------------------------------
# 智能审批 cloudflow：会议纪要审批流抓取
# ---------------------------------------------------------------------------
_CLOUDFLOW_BASE = f"{API_BASE}/gateway/workflow/form/thirdpart"


def _cloudflow_post(path, body, access_token=None):
    """cloudflow 接口统一 POST：accessToken 拼 QueryString。"""
    tok = access_token or get_access_token()
    url = f"{_CLOUDFLOW_BASE}/{path}?accessToken={tok}"
    r = requests.post(url, json=body, headers=_HEADERS_JSON, timeout=20)
    r.raise_for_status()
    data = r.json()
    # getTemplates 直接返回数组(list)；其余接口返回 {success,data,...}
    if isinstance(data, list):
        return data
    if not data.get("success"):
        raise RuntimeError("cloudflow %s 失败：%s" % (path, r.text[:400]))
    return data.get("data", {})


def get_templates():
    """获取工作圈全部审批模板（扁平化）。

    返回 [{formCodeId, title, category, available, state}, ...]。
    模板真实字段：title=模板名，codeId=模板codeId(formCodeId)。
    """
    cats = _cloudflow_post("getTemplates", {})
    out = []
    for cat in cats:
        cat_name = cat.get("name") or ""
        for ft in (cat.get("formTemplates") or []):
            out.append({
                "formCodeId": ft.get("codeId"),
                "title": ft.get("title"),
                "category": cat_name,
                "available": ft.get("available"),
                "state": ft.get("state"),
            })
    return out


def find_flows(form_code_ids=None, status=None, title=None, page_number=1,
               page_size=50, create_time=None):
    """按模板/状态拉流程实例列表。

    form_code_ids: 模板 codeId 数组；status: ['RUNNING'|'FINISH'...]；
    create_time: [start_ts_ms, end_ts_ms]。
    返回 { list:[{flowInstId, formInstId, formCodeId, title, status, creator,...}], total }
    """
    body = {
        "pageNumber": page_number,
        "pageSize": page_size,
    }
    if form_code_ids:
        body["formCodeIds"] = form_code_ids
    if status:
        body["status"] = status if isinstance(status, list) else [status]
    if title:
        body["title"] = title
    if create_time:
        body["createTime"] = create_time
    return _cloudflow_post("findFlows", body)


def view_form_inst(form_inst_id, form_code_id):
    """按表单实例 id + 模板 codeId 读单据全部字段。

    返回 data.formInfo: { widgetMap:{控件code: {value,...}}, detailMap:{...} }
    """
    body = {"formInstId": form_inst_id, "formCodeId": form_code_id}
    return _cloudflow_post("viewFormInst", body)


# ---------------------------------------------------------------------------
# 文件下载（红头/纪要 docx|pdf）：需 resGroupSecret 级 token
# ---------------------------------------------------------------------------
def get_file_access_token():
    """获取文件服务 accessToken（scope=resGroupSecret），缓存复用。

    需要 .env 的 YUNZHIJIA_RESGROUP_SECRET（管理中心->文件服务上传下载密钥）。
    """
    global _TOKENS
    scope = "resGroupSecret"
    cached = _TOKENS.get(scope)
    if cached and time.time() < cached[1] - 60:
        return cached[0]
    if not RESGROUP_SECRET:
        raise RuntimeError("缺少 YUNZHIJIA_RESGROUP_SECRET（文件服务密钥），无法下载附件")
    url = f"{API_BASE}/gateway/oauth2/token/getAccessToken"
    body = {
        "eid": EID,
        "secret": RESGROUP_SECRET,
        "timestamp": int(time.time() * 1000),
        "scope": scope,
    }
    r = requests.post(url, json=body, headers=_HEADERS_JSON, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError("获取文件 accessToken 失败：%s" % r.text[:400])
    d = data.get("data", {})
    tok = d.get("accessToken")
    if not tok:
        raise RuntimeError("获取文件 accessToken 失败：%s" % r.text[:400])
    _TOKENS[scope] = (tok, time.time() + int(d.get("expireIn", 7200)), None)
    return tok


def get_red_head_file(red_head_id):
    """按红头文件实例 id 取真实 fileId / pdfFileId（team token，query 传 accessToken）。

    red_head_id 来自表单控件 onlineDocumentWidget 的 redFileId / sealedFileId。
    """
    return _cloudflow_post("getRedHeadFile", {"id": red_head_id}).get("data", {})


def download_file(file_id, save_path):
    """下载文件字节流到本地（resGroupSecret token，header x-accessToken）。

    返回保存路径；失败抛异常。
    """
    tok = get_file_access_token()
    url = f"{API_BASE}/docrest/doc/user/downloadfile"
    headers = {"x-accessToken": tok}
    params = {"bizkey": "cloudflow", "fileId": file_id}
    r = requests.get(url, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(r.content)
    return save_path


if __name__ == "__main__":
    import json as _json
    try:
        at = get_access_token()
        print("[YZJ] 鉴权成功，accessToken 前缀:", at[:20] + "...")
        print("\n[YZJ] 查找目标会议纪要模板 ...")
        tpls = get_templates()
        targets = ["总经理会议纪要线上审批", "会议纪要线上审批发布"]
        code_map = {}
        for t in tpls:
            if t.get("title") in targets:
                code_map[t["title"]] = t["formCodeId"]
                print("  [命中] %s -> code=%s | 分类:%s"
                      % (t["title"], t["formCodeId"], t["category"]))
        if not code_map:
            print("[YZJ] 未命中目标模板，现有模板名：",
                  [t.get("title") for t in tpls])
        else:
            for name, code in code_map.items():
                print("\n===== %s (code=%s) =====" % (name, code))
                flows = find_flows(form_code_ids=[code], status="FINISH",
                                   page_size=5)
                flow_list = flows.get("list", []) if isinstance(flows, dict) else flows
                print("  已完成流程实例数(本页):", len(flow_list))
                if flow_list:
                    f0 = flow_list[0]
                    print("  首条:", _json.dumps(f0, ensure_ascii=False)[:400])
                    fiid = f0.get("formInstId")
                    if fiid:
                        print("  >>> 读取首条表单字段 viewFormInst ...")
                        inst = view_form_inst(fiid, code)
                        wm = inst.get("formInfo", {}).get("widgetMap", {})
                        od = wm.get("Od_0", {})
                        files = od.get("value", []) or []
                        print("  [Od_0 会议纪要文件] 数量:", len(files))
                        for i, fmeta in enumerate(files):
                            print("    文件%d: name=%s sealedFileId=%s redFileId=%s wpsFileId=%s"
                                  % (i, fmeta.get("wpsFileName") or fmeta.get("sealedFileName"),
                                     fmeta.get("sealedFileId"), fmeta.get("redFileId"),
                                     fmeta.get("wpsFileId")))
                            sfid = fmeta.get("sealedFileId")
                            if sfid:
                                # 试下载盖章 pdf（需 resGroupSecret token）
                                try:
                                    out = "yzj_meeting_%s_%d.pdf" % (f0.get("serialNo", i), i)
                                    p = download_file(sfid, out)
                                    print("    [下载成功] ->", p)
                                except Exception as de:
                                    print("    [下载失败] %s" % de)
    except Exception as e:
        print("[YZJ] 失败:", repr(e))
