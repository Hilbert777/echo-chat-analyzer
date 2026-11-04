"""Group member browsing preserves scope and original-message identity."""
from dataclasses import replace
import html
import json
import re

import pytest

from qq_chat_analyzer.presentation import export_echo_report_html
from qq_chat_analyzer.presentation.models import (
    EchoLanguageMember, EchoLanguageProfile, EchoMemberExpression,
    EchoExpressionCulture, EchoExpressionItem,
    EchoConversationSessions, EchoSessionInitiator, EchoStickers, EchoStickerMember,
)
from test_language_sources import _view, _message, _build
from test_echo_conversation_sessions_frontend import _find_chromium, _run_browser


@pytest.mark.slow_integration
@pytest.mark.parametrize("width,kind", [(1280, "group"), (390, "private")])
def test_member_sort_search_and_original_identity_in_offline_report(tmp_path, width, kind):
    chromium = _find_chromium()
    if chromium is None:
        pytest.skip("no local Chromium available")
    names = ("陈果", "阿青", "白果") if kind == "group" else ("陈果", "阿青")
    template = _view().members[0]
    members = tuple(replace(template, speaker_key=f"fictional-{i}", display_name=name,
        message_count=(20, 5, 100)[i], first_seen_order=(0, 1, 2)[i],
        remark="小白" if i == 2 else None,
        contextual_name="虚构群名片" if i == 2 else None,
        nickname="虚构全局昵称" if i == 2 else None) for i, name in enumerate(names))
    language = EchoLanguageProfile("group_distinctive" if kind == "group" else "private_common", True,
        members=tuple(EchoLanguageMember(m.speaker_key, m.display_name, m.display_name, ("苹果",))
                      for m in members))
    expression = EchoExpressionItem("😀", "😀", 2, "unicode", 0, 2)
    culture = EchoExpressionCulture(True, expression_message_count=6, top_expressions=(expression,),
        members=tuple(EchoMemberExpression(m.speaker_key, m.display_name, (2, 80, 10)[i],
            2, 20, 2, (expression,)) for i, m in enumerate(members)))
    view = replace(_view(), conversation_kind=kind, members=members,
                   language_profile=language, expression_culture=culture)
    if kind == "group":
        view = replace(view, conversation_sessions=EchoConversationSessions(
            1800, 3, 0, 0, 0, 1, group_initiators=tuple(
                EchoSessionInitiator(m.display_name, 1, False, m.search_names) for m in members)),
            stickers=EchoStickers(available=True, conversation_kind="group", total_send_count=3,
                members=tuple(EchoStickerMember(m.display_name, 1, search_names=m.search_names) for m in members)))
    messages = [_message(f"Fictional body {i} 苹果", index=i, sender=m.speaker_key)
                for i, m in enumerate(members)]
    view = replace(view, language_sources=_build(messages, [["苹果"]] * len(messages), view=view))
    output = export_echo_report_html(view, tmp_path / "members.html")
    probe = r"""<script>window.addEventListener('load',()=>{
      const r={};
      try {
        const tools=['rhythm','voices','expression'].map(id=>document.getElementById(id+'-member-tools'));
        r.tools=tools.map(t=>!!t&&!t.hidden);
        if(window.ECHO_DATA.conversation.kind==='group') {
          const keys=id=>Array.from(document.getElementById(id).children).filter(n=>!n.hidden).map(n=>n.dataset.speakerKey);
          r.initial=keys('member-list');r.expressionInitial=keys('expression-members');
          const order=tools[1].querySelector('select');order.value='name';order.dispatchEvent(new Event('change'));
          r.byName=keys('member-list');
          const word=document.querySelector('#member-list .voice-entry:not([hidden]) .language-word-button');
          word.click();r.original=document.querySelector('.language-original-text').textContent;
          document.getElementById('language-originals').close();
          order.value='first';order.dispatchEvent(new Event('change'));r.byFirst=keys('member-list');
          const search=tools[1].querySelector('input');search.value='小白';search.dispatchEvent(new Event('input'));
          r.byAlias=keys('member-list');search.value='not-a-person';search.dispatchEvent(new Event('input'));
          r.allAliases=['小白','虚构群名片','虚构全局昵称'].map(alias=>{
            const result={};
            ['rhythm','voices','expression'].forEach((id,i)=>{
              const input=tools[i].querySelector('input');input.value=alias;input.dispatchEvent(new Event('input'));
              result[id]=i===0?Array.from(document.getElementById('rhythm-member').options).filter(n=>!n.hidden&&n.value).map(n=>n.value):keys(i===1?'member-list':'expression-members');
            });
            const opener=document.getElementById('session-openers-search');opener.value=alias;opener.dispatchEvent(new Event('input'));
            result.openers=Array.from(document.getElementById('session-openers-list').children).filter(n=>!n.hidden).length;
            const sticker=document.getElementById('sticker-member-search');sticker.value=alias;sticker.dispatchEvent(new Event('input'));
            result.stickers=document.getElementById('sticker-member-options').children.length;
            return result;
          });
          tools.forEach(tool=>{const input=tool.querySelector('input');input.value='';input.dispatchEvent(new Event('input'));});
          search.value='not-a-person';search.dispatchEvent(new Event('input'));
          r.empty=keys('member-list').length===0&&!document.getElementById('voices-member-empty').hidden;
          search.value='';search.dispatchEvent(new Event('input'));r.restored=keys('member-list').length===3;
          const exprOrder=tools[2].querySelector('select');exprOrder.value='expressions';
          exprOrder.dispatchEvent(new Event('change'));r.byExpression=keys('expression-members');
          const select=document.getElementById('rhythm-member');select.value='fictional-0';
          select.dispatchEvent(new Event('change'));const label=document.getElementById('rhythm-scope-label').textContent;
          const rhythmSearch=tools[0].querySelector('input');rhythmSearch.value='小白';rhythmSearch.dispatchEvent(new Event('input'));
          r.rhythmScopeStable=select.value==='fictional-0'&&document.getElementById('rhythm-scope-label').textContent===label;
          r.rhythmMatches=Array.from(select.options).filter(o=>!o.hidden).map(o=>o.value);
          rhythmSearch.value='';rhythmSearch.dispatchEvent(new Event('input'));
          r.rhythmRestored=select.options.length===4&&select.value==='fictional-0';
          document.getElementById('rhythm-private-scopes').hidden=true;
        }
        r.overflow=document.documentElement.scrollWidth>innerWidth+1;
        r.noScriptInjection=!window.fictionalAttack;
      }catch(e){r.error=String(e)}
      const pre=document.createElement('pre');pre.id='member-probe';pre.hidden=true;
      pre.textContent=JSON.stringify(r);document.body.append(pre);
    });</script>"""
    output.write_text(output.read_text(encoding="utf-8").replace("</body>", probe + "</body>"), encoding="utf-8")
    completed = _run_browser([chromium, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND",
        f"--user-data-dir={tmp_path / 'browser'}", f"--window-size={width},900",
        "--virtual-time-budget=2000", "--dump-dom", output.as_uri()])
    assert completed.returncode == 0
    match = re.search(r'<pre id="member-probe" hidden="">(.*?)</pre>', completed.stdout.decode("utf-8"), re.S)
    assert match is not None
    result = json.loads(html.unescape(match.group(1)))
    assert "error" not in result, result
    assert result["tools"] == [kind == "group"] * 3
    if kind == "group":
        assert result["initial"] == result["expressionInitial"] == ["fictional-2", "fictional-0", "fictional-1"]
        assert result["byName"] == ["fictional-1", "fictional-2", "fictional-0"]
        assert result["original"] == "Fictional body 1 苹果"
        assert result["byFirst"] == ["fictional-0", "fictional-1", "fictional-2"]
        assert result["byAlias"] == ["fictional-2"]
        assert result["allAliases"] == [dict(rhythm=["fictional-2"], voices=["fictional-2"],
            expression=["fictional-2"], openers=1, stickers=1)] * 3
        assert result["byExpression"] == ["fictional-1", "fictional-2", "fictional-0"]
        assert result["empty"] and result["restored"] and result["rhythmScopeStable"] and result["rhythmRestored"]
        assert result["rhythmMatches"] == ["", "fictional-2"]
    assert not result["overflow"] and result["noScriptInjection"]
