"""Lightweight group-member sorting and lookup over existing report nodes."""

ECHO_MEMBER_CONTROLS_CSS = r"""
.member-tools { display: flex; justify-content: flex-end; align-items: center; flex-wrap: wrap; gap: 10px; margin: 18px 0 12px; color: var(--muted); font: 11px var(--sans); }
.member-tools[hidden], .rhythm-group-controls[hidden], .member-search-empty[hidden], #member-list > [hidden], #expression-members > [hidden] { display: none !important; }
.member-tools label { display: flex; align-items: center; gap: 6px; }
.member-tools select, .member-tools input { min-width: 0; max-width: 100%; border: 0; border-bottom: 1px solid var(--rule); border-radius: 0; padding: 6px 2px; background: transparent; color: var(--muted); font: inherit; }
.member-tools input { width: 126px; }
.member-tools input::placeholder { color: var(--faint); }
.member-search-empty { margin: 16px 0; color: var(--muted); font: 12px/1.8 var(--sans); }
.rhythm-toolbar { flex-wrap: wrap; }
.rhythm-group-controls { display: flex; justify-content: flex-end; align-items: center; flex-wrap: wrap; gap: 10px; min-width: 0; }
.rhythm-group-controls .member-tools { margin: 0; }
#rhythm-group-controls #rhythm-member { max-width: 180px; }
#expression-member-tools { margin-top: 26px; }
#expression-member-tools:not([hidden]) ~ .expression-members { margin-top: 10px; }
@media (max-width: 760px) {
  .rhythm-group-controls { flex-basis: 100%; }
}
@media print {
  .member-tools, .rhythm-group-controls, .member-search-empty { display: none !important; }
  #member-list > [hidden], #expression-members > [hidden] { display: block !important; }
}
"""

ECHO_MEMBER_CONTROLS_JS = r"""
(function () {
  "use strict";
  var data = window.ECHO_DATA;
  if (!data || !data.conversation || data.conversation.kind !== "group") return;
  var members = new Map((data.members || []).map(function (member, index) {
    return [member.speaker_key, {member: member, index: index}];
  }));
  var expressions = new Map(((data.expression_culture || {}).members || []).map(function (member) {
    return [member.speaker_key, member.expression_occurrence_count];
  }));
  var names = new Intl.Collator("zh-CN", {numeric: true, sensitivity: "base"});
  function normalized(text) { return String(text || "").normalize("NFKC").toLocaleLowerCase("zh-CN"); }
  [
    {id: "rhythm", list: "rhythm-member", select: true},
    {id: "voices", list: "member-list"},
    {id: "expression", list: "expression-members"}
  ].forEach(function (section) {
    var tools = document.getElementById(section.id + "-member-tools");
    var list = document.getElementById(section.list);
    if (!tools || !list || (section.id === "voices" && !(data.language_profile || {}).available) ||
        (section.id === "expression" && !(data.expression_culture || {}).available)) return;
    var nodes = Array.from(list.children);
    var overall = section.select ? nodes.find(function (node) { return node.value === ""; }) : null;
    var entries = nodes.filter(function (node) { return node !== overall; }).map(function (node, index) {
      var key = section.select ? node.value : node.dataset.speakerKey;
      var info = members.get(key), member = info ? info.member : {};
      return {node: node, key: key, member: member, index: info ? info.index : index,
        search: [member.display_name].concat(member.search_names || []).map(normalized).join("\n")};
    });
    if (!entries.length) return;
    tools.hidden = false;
    if (section.select) document.getElementById("rhythm-group-controls").hidden = false;
    var label = document.createElement("label");label.textContent = "排列";
    var order = document.createElement("select");order.setAttribute("aria-label", "成员排列顺序");
    [["messages", "按发言量"], ["name", "按昵称"], ["first", "按最早出现"]].concat(
      section.id === "expression" ? [["expressions", "按表达次数"]] : []
    ).forEach(function (choice) {
      var option = document.createElement("option");option.value = choice[0];option.textContent = choice[1];
      if (choice[0] === "first") option.disabled = !entries.some(function (entry) {
        return Number.isInteger(entry.member.first_seen_order);
      });
      order.appendChild(option);
    });
    label.appendChild(order);tools.appendChild(label);
    var search = document.createElement("input");search.type = "search";search.placeholder = "查找成员…";
    search.setAttribute("aria-label", "查找成员昵称、备注或群名片");tools.appendChild(search);
    function render() {
      var query = normalized(search.value.trim()), mode = order.value;
      var selected = section.select ? list.value : null;
      var ordered = entries.slice().sort(function (a, b) {
        var difference = mode === "name" ? names.compare(a.member.display_name || "", b.member.display_name || "") :
          mode === "first" ? (a.member.first_seen_order ?? Infinity) - (b.member.first_seen_order ?? Infinity) :
          mode === "expressions" ? (expressions.get(b.key) || 0) - (expressions.get(a.key) || 0) :
          (b.member.message_count || 0) - (a.member.message_count || 0);
        return difference || a.index - b.index;
      });
      var shown = 0;
      ordered.forEach(function (entry) {
        var matches = !query || entry.search.indexOf(query) >= 0;
        entry.node.hidden = !matches;
        if (matches) shown++;
        if (section.select) {
          // Keep the selected scope even when the search hides its option.
          if (matches || entry.key === selected) list.appendChild(entry.node);
          else entry.node.remove();
        } else {
          list.appendChild(entry.node);
          var number = entry.node.querySelector(".member-index");
          if (number && matches) number.textContent = String(shown).padStart(2, "0");
        }
      });
      if (section.select) list.value = selected;
      document.getElementById(section.id + "-member-empty").hidden = shown !== 0;
    }
    order.addEventListener("change", render);search.addEventListener("input", render);render();
  });
})();
"""
