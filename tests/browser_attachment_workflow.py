"""Exercise attachment interactions against a deterministic Plugin Page Bridge.

Run with a Python environment containing Playwright and installed browsers:
    python tests/browser_attachment_workflow.py --artifacts /workspace/artifacts/email-attachments
"""

import argparse
import functools
import http.server
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


FIXTURE = r"""
(() => {
  const account = {account_id:'one', name:'个人邮箱', query_enabled:true, send_enabled:true,
    runtime_status:{ok:true,last_success_at:1793851200,consecutive_failures:0}, index:{active:2}};
  const original = {account_id:'one',folder:'INBOX',uid:7,subject:'带附件的项目资料',
    from_addr:'team@example.com',date:'2026-11-05',body:'请查看本次项目资料和设计图片。',has_attachments:true};
  const file = {part_id:'2',filename:'项目资料_abcdefghijklmnopqrstuvwxyz_0123456789_正式版本.pdf',
    content_type:'application/pdf',size_bytes:4096};
  let drafts = [{draft_id:'existing',account_id:'one',to_addrs:['reader@example.com'],cc_addrs:[],bcc_addrs:[],
    subject:'已保存的草稿',body_text:'原始正文',status:'editing',revision:1,attachments:[],attachment_ids:[]}];
  const attachments = {};
  window.fixture = {calls:[], downloads:[], holdUpload:false, rejectUpload:false, failAttachmentList:false,
    holdAttachmentList:false,holdSave:false};
  const clone = (value) => JSON.parse(JSON.stringify(value));
  window.AstrBotPluginPage = {
    ready:async()=>({theme:window.fixtureTheme || 'light',locale:'zh-CN'}),
    getLocale:()=> 'zh-CN',
    apiGet:async(path,params={})=> {
      if(path==='overview') return {plugin:{index_enabled:true},accounts:[account],attachment_cache:{total_bytes:4096,max_total_bytes:1073741824,retention_days:90}};
      if(path==='folders') return {primary_folder:'INBOX',items:[{name:'INBOX',display_name:'收件箱',selectable:true}]};
      if(path==='messages') return {items:[{...original,from_name:'项目组',date_ts:1793851200}],has_more:false};
      if(path==='message') return clone(original);
      if(path==='message/attachments') {
        if(window.fixture.holdAttachmentList) await new Promise(resolve=>window.fixture.releaseAttachmentList=resolve);
        if(window.fixture.failAttachmentList) throw new Error('附件清单暂不可用');
        return {items:[file]};
      }
      if(path==='drafts') return {items:clone(drafts)};
      if(path==='draft') return clone(drafts.find(d=>d.draft_id===params.draft_id));
      throw new Error(`Unexpected GET ${path}`);
    },
    apiPost:async(path,body)=> {
      window.fixture.calls.push({path,body:clone(body)});
      if(window.fixture.holdSave) await new Promise(resolve=>window.fixture.releaseSave=resolve);
      if(path==='drafts/create') {
        const d={...body,draft_id:'new',revision:1,status:'editing'};
        d.to_addrs=body.to_addrs.split(',');d.attachments=body.attachment_ids.map(id=>attachments[id]);
        drafts.push(d);return clone(d);
      }
      const d=drafts.find(d=>d.draft_id===body.draft_id);
      if(path==='drafts/update') {
        if(d.revision!==body.revision) throw new Error('草稿已修改');
        Object.assign(d,body,{status:'editing',revision:d.revision+1});
        d.to_addrs=body.to_addrs.split(',');d.attachments=body.attachment_ids.map(id=>attachments[id]);return clone(d);
      }
      if(path==='drafts/approve'){d.status='approved';d.revision++;return clone(d);}
      if(path==='drafts/send'){d.status='sent';d.revision++;return clone(d);}
      throw new Error(`Unexpected POST ${path}`);
    },
    upload:async(endpoint,file)=>{
      window.fixture.calls.push({path:endpoint,filename:file.name});
      if(window.fixture.holdUpload) await new Promise(resolve=>window.fixture.releaseUpload=resolve);
      if(window.fixture.rejectUpload || file.name==='failure.txt') throw new Error('附件缓存容量不足');
      const id=`attachment-${Object.keys(attachments).length}`;
      const item={attachment_id:id,filename:file.name,content_type:file.type,size_bytes:file.size};
      attachments[id]=item;
      const [_,__,accountId,draftId,revision]=endpoint.split('/');
      const draft=drafts.find(d=>d.draft_id===draftId);
      if(draft.revision!==Number(revision)) throw new Error('草稿已修改');
      draft.attachments.push(item);draft.attachment_ids.push(id);draft.revision++;draft.status='editing';
      return clone(draft);
    },
    download:async(endpoint,params,filename)=>{window.fixture.downloads.push({endpoint,params,filename});},
  };
})();
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def run_case(browser, address, output, width, height, theme, name):
    page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=theme, reduced_motion="reduce")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.route("**/favicon.ico", lambda route: route.fulfill(status=204))
    page.add_init_script(f"window.fixtureTheme={theme!r};")
    page.add_init_script(FIXTURE)
    page.goto(address)
    expect(page.locator("#runtime-state")).to_contain_text("索引就绪")
    page.locator(".message-item").click()
    expect(page.locator("#message-detail .attachment-row")).to_have_count(1)
    page.locator("#message-detail .attachment-row button").click()
    page.wait_for_function("window.fixture.downloads.length===1")
    page.screenshot(path=str(output / f"{name}-mail.png"), full_page=True)
    assert_no_overflow(page)

    # A late list response cannot reinsert an old attachment section after navigation.
    page.evaluate("window.fixture.holdAttachmentList=true")
    if width <= 700:
        page.locator(".detail-back-button").click()
    page.locator(".message-item").click()
    page.wait_for_function("!!window.fixture.releaseAttachmentList")
    if width <= 900:
        page.locator("#nav-toggle").click()
    page.locator('[data-tab="drafts"]').click()
    page.evaluate("window.fixture.releaseAttachmentList();window.fixture.holdAttachmentList=false")
    expect(page.locator("#draft-heading")).to_have_text("新草稿")
    page.locator("#draft-to").fill("reader@example.com")
    page.locator("#draft-subject").fill("包含文件和图片的草稿")
    page.locator("#draft-body").fill("上传附件时必须保留的手动编辑正文。")
    page.evaluate("window.fixture.holdUpload=true")
    page.locator("#draft-attachment-input").set_input_files([
        {"name": "很长的文件名_abcdefghijklmnopqrstuvwxyz_0123456789_项目说明.txt", "mimeType": "text/plain", "buffer": b"project"},
        {"name": "图片.png", "mimeType": "image/png", "buffer": b"fixture-image"},
    ])
    expect(page.locator("#attachment-progress")).to_contain_text("正在上传 1/2")
    expect(page.locator("#draft-body")).to_be_disabled()
    expect(page.locator("#new-draft-button")).to_be_disabled()
    page.locator(".draft-item").first.click()
    expect(page.locator("#draft-body")).to_have_value("上传附件时必须保留的手动编辑正文。")
    page.evaluate("window.fixture.holdUpload=false;window.fixture.releaseUpload()")
    expect(page.locator("#draft-attachments .attachment-row")).to_have_count(2)
    expect(page.locator("#draft-body")).to_be_enabled()
    expect(page.locator("#draft-body")).to_have_value("上传附件时必须保留的手动编辑正文。")
    page.locator("#draft-body").fill("上传附件时必须保留的手动编辑正文。补充内容。")
    page.locator("#new-draft-button").click()
    expect(page.locator("#modal-title")).to_have_text("放弃未保存的修改？")
    page.keyboard.press("Escape")
    expect(page.locator("#draft-attachments .attachment-row")).to_have_count(2)

    # The first successful upload remains visible when a later file fails.
    page.locator("#draft-attachment-input").set_input_files([
        {"name": "第三个附件.txt", "mimeType": "text/plain", "buffer": b"ok"},
        {"name": "failure.txt", "mimeType": "text/plain", "buffer": b"fail"},
    ])
    expect(page.locator("#attachment-progress")).to_contain_text("已保留 1 个")
    expect(page.locator("#draft-attachments .attachment-row")).to_have_count(3)
    page.screenshot(path=str(output / f"{name}-draft-error.png"), full_page=True)
    assert_no_overflow(page)
    page.locator("#draft-attachments [data-remove-attachment]").last.click()
    page.evaluate("window.fixture.holdSave=true")
    page.locator("#save-draft-button").click()
    expect(page.locator("#draft-body")).to_be_disabled()
    page.locator("#save-draft-button").evaluate("button=>button.click()")
    page.wait_for_function("!!window.fixture.releaseSave")
    page.evaluate("window.fixture.holdSave=false;window.fixture.releaseSave()")
    expect(page.locator("#draft-status")).to_have_text("编辑中")
    assert page.evaluate("window.fixture.calls.filter(c=>c.path==='drafts/create').length") == 1
    payload = page.evaluate("window.fixture.calls.filter(c=>c.path==='drafts/update').at(-1).body")
    assert len(payload["attachment_ids"]) == 2
    assert payload["body_text"] == "上传附件时必须保留的手动编辑正文。补充内容。"
    page.locator("#approve-draft-button").click()
    expect(page.locator("#send-draft-button")).to_be_enabled()
    page.locator("#send-draft-button").click()
    expect(page.locator("#modal-message")).to_contain_text("附件 2 个")
    expect(page.locator("#modal-message")).to_contain_text("图片.png")
    page.screenshot(path=str(output / f"{name}-confirm.png"))
    assert_no_overflow(page)
    page.keyboard.press("Escape")
    expect(page.locator("#send-draft-button")).to_be_focused()
    page.locator("#draft-attachments [data-remove-attachment]").first.click()
    expect(page.locator("#send-draft-button")).to_be_disabled()
    page.locator("#save-draft-button").click()
    expect(page.locator("#draft-status")).to_have_text("编辑中")
    page.locator("#approve-draft-button").click()
    expect(page.locator("#send-draft-button")).to_be_enabled()
    page.locator("#send-draft-button").click()
    page.locator("#modal-confirm").click()
    expect(page.locator("#draft-status")).to_have_text("已发送")
    expect(page.locator("#add-attachment-button")).to_be_disabled()
    expect(page.locator("#draft-attachments [data-remove-attachment]")).to_be_disabled()
    assert page.evaluate("window.fixture.calls.filter(c=>c.path==='drafts/send').length") == 1
    assert not errors, errors
    page.close()


def assert_no_overflow(page):
    geometry = page.evaluate("({viewport:document.documentElement.clientWidth,width:document.documentElement.scrollWidth})")
    assert geometry["width"] <= geometry["viewport"] + 1, geometry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1] / "pages" / "mailbox"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as runner:
            for engine, sizes in (
                ("chromium", [(1440, 1000), (900, 1000), (390, 844), (320, 700)]),
                ("webkit", [(390, 844)]),
            ):
                browser = getattr(runner, engine).launch()
                try:
                    for width, height in sizes:
                        for theme in ("light", "dark"):
                            name = f"{engine}-{width}-{theme}"
                            run_case(browser, f"http://127.0.0.1:{server.server_port}/", args.artifacts, width, height, theme, name)
                            print(f"PASS {name}", flush=True)
                finally:
                    browser.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
