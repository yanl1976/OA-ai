"""按云之家开放平台文档拉取企业全部在职人员（通讯录）。

接口（通讯录同步能力，resGroupSecret 级 token）：
  POST {API_BASE}/gateway/openimport/open/person/getall?accessToken=xxx
  Content-Type: application/x-www-form-urlencoded
  表单参数: eid(必传), data(必传, JSON 字符串, 如 {"begin":0,"count":1000}), nonce(可选)
  分页: 返回条数 < count 表示拉取结束

密钥：
  YUNZHIJIA_CONTACT_SECRET  管理中心-系统设置-系统集成-通讯录同步 的只读密钥（推荐）
  缺省回退 YUNZHIJIA_RESGROUP_SECRET（注意：该值默认是文件服务密钥，调通讯录会 10000401 认证失败）
"""
import os, sys, json, time, pathlib

# 本脚本位于 scripts/contact/，需把 app/ 加入模块搜索路径以复用 yunzhijia_client（鉴权 + .env 读取）
_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / 'app'))
import yunzhijia_client as y  # noqa: E402
import requests  # noqa: E402

API_BASE = y.API_BASE
EID = y.EID
CONTACT_SECRET = os.environ.get('YUNZHIJIA_CONTACT_SECRET') or y.RESGROUP_SECRET


def get_contact_token():
    """用通讯录同步密钥换取 resGroupSecret 级 accessToken。"""
    url = f'{API_BASE}/gateway/oauth2/token/getAccessToken'
    body = {
        'eid': EID,
        'secret': CONTACT_SECRET,
        'timestamp': int(time.time() * 1000),
        'scope': 'resGroupSecret',
    }
    r = requests.post(url, json=body, headers={'Content-Type': 'application/json'}, timeout=15)
    j = r.json()
    if not j.get('success'):
        raise RuntimeError('通讯录 accessToken 获取失败: ' + json.dumps(j, ensure_ascii=False)[:300])
    return (j.get('data') or {}).get('accessToken')


def getall(token, begin, count):
    url = f'{API_BASE}/gateway/openimport/open/person/getall?accessToken={token}'
    form = {'eid': EID, 'data': json.dumps({'begin': begin, 'count': count}, ensure_ascii=False)}
    r = requests.post(url, data=form, timeout=30)
    r.raise_for_status()
    return r.json()


def extract_persons(j):
    """兼容 data 为数组或 {persons|list|data|items:[...]} 的返回结构。"""
    d = j.get('data')
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ('persons', 'list', 'data', 'items'):
            if isinstance(d.get(k), list):
                return d[k]
    return []


def main():
    token = get_contact_token()
    print('[token] 通讯录 resGroupSecret token 获取成功, EID=%s' % EID)

    all_persons = []
    begin, count = 0, 1000
    while True:
        resp = getall(token, begin, count)
        meta = {k: resp.get(k) for k in ('success', 'error', 'errorCode')}
        persons = extract_persons(resp)
        print('[page] begin=%d -> %s count=%d' % (begin, meta, len(persons)))
        if not persons:
            if meta.get('success') is False:
                print('[error] 接口返回失败，原始响应:', json.dumps(resp, ensure_ascii=False)[:400])
            break
        all_persons.extend(persons)
        if len(persons) < count:
            break
        begin += len(persons)
        time.sleep(0.2)

    print('TOTAL persons:', len(all_persons))
    if all_persons:
        print('sample keys:', list(all_persons[0].keys()))
        print('sample:', json.dumps(all_persons[0], ensure_ascii=False)[:600])

    out = pathlib.Path(__file__).resolve().parent / 'data' / 'contact_persons.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(all_persons, ensure_ascii=False, indent=2), encoding='utf-8')
    print('saved ->', out)


if __name__ == '__main__':
    main()
