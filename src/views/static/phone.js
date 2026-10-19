/* Phone boxes, same rule as phone.py on the server:
     <input data-phone="mobile">  → 081-234-5678  (10 digits)
     <input data-phone="tel">     → 0-2034-4147   (9 digits, landline)
   Digits only; a +66 country code becomes 0. Formats while typing and on page load,
   and marks a box with the wrong number of digits invalid so a form won't submit.
   Rows built later (Manage users table) are covered by the delegated listeners; use
   PhoneFmt.format(value, kind) to pre-format a value in HTML you build. */
(function(){
  const LEN = { mobile: 10, tel: 9 };
  const HINT = { mobile: "Mobile: 10 digits — xxx-xxx-xxxx", tel: "Tel: 9 digits — x-xxxx-xxxx" };
  function digits(v, kind){
    v = String(v || "");
    let d = v.replace(/\D/g, "");
    if(/^\s*\+\s*6\s*6/.test(v) || (d.startsWith("66") && d.length === LEN[kind] + 1)) d = "0" + d.slice(2);
    return d.slice(0, LEN[kind]);
  }
  function shape(d, kind){
    if(kind === "tel") return d.length > 5 ? `${d.slice(0,1)}-${d.slice(1,5)}-${d.slice(5)}`
                            : d.length > 1 ? `${d.slice(0,1)}-${d.slice(1)}` : d;
    return d.length > 6 ? `${d.slice(0,3)}-${d.slice(3,6)}-${d.slice(6)}`
         : d.length > 3 ? `${d.slice(0,3)}-${d.slice(3)}` : d;
  }
  const format = (v, kind) => shape(digits(v, kind), kind);
  function validate(inp){
    const kind = inp.dataset.phone, n = digits(inp.value, kind).length;
    inp.setCustomValidity(n && n !== LEN[kind] ? HINT[kind] : "");
    inp.title = HINT[kind];
  }
  function onInput(inp){
    const kind = inp.dataset.phone;
    if(/^\s*\+\s*6?\s*$/.test(inp.value)){ inp.value = inp.value.replace(/\s/g, ""); return; }   // "+", "+6" → wait for "+66"
    const before = inp.value.slice(0, inp.selectionStart).replace(/\D/g, "").length;
    inp.value = format(inp.value, kind);
    let pos = 0, seen = 0;                       // keep the caret after the same digit
    while(pos < inp.value.length && seen < before){ if(/\d/.test(inp.value[pos])) seen++; pos++; }
    if(document.activeElement === inp) inp.setSelectionRange(pos, pos);
    validate(inp);
  }
  function setup(inp){
    if(inp.dataset.phoneReady) return;
    inp.dataset.phoneReady = "1";
    inp.setAttribute("inputmode", "numeric");
    inp.maxLength = inp.dataset.phone === "tel" ? 11 : 12;
    if(inp.value) inp.value = format(inp.value, inp.dataset.phone);
    validate(inp);
  }
  document.addEventListener("input", e => { if(e.target.matches && e.target.matches("input[data-phone]")){ setup(e.target); onInput(e.target); } });
  document.addEventListener("focusin", e => { if(e.target.matches && e.target.matches("input[data-phone]")) setup(e.target); });
  const all = () => document.querySelectorAll("input[data-phone]").forEach(setup);
  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", all); else all();
  window.PhoneFmt = { format, setupAll: all };
})();
