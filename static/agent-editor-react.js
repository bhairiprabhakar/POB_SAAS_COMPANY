/*
 * React proof-of-concept for the verification editor.
 * Written with React.createElement directly (no JSX, no Babel-in-browser,
 * no bundler) so this can ship as a single static file with zero build
 * pipeline -- appropriate for a first proof-of-concept. If this pattern
 * proves out and you want to expand React to more pages, that's the point
 * to introduce a proper build step (Vite is the standard recommendation)
 * so you can write JSX, split components into files, and get HMR in dev.
 *
 * Talks to the exact same JSON endpoints the Jinja/vanilla-JS version uses:
 *   GET  /agent/verification/<id>/party-items
 *   POST /agent/verification/<id>/inline-save
 *   POST /agent/verification/<id>/reject
 *   POST /agent/verification/<id>/reextract
 * No new backend surface for this page.
 */
(function () {
  var h = React.createElement;
  var MV_ID = window.__MV_ID__;
  var MV = window.__MV__ || {};

  function useParties() {
    var state = React.useState({ loading: true, error: null, parties: [] });
    var data = state[0], setData = state[1];

    function reload() {
      setData(function (d) { return Object.assign({}, d, { loading: true }); });
      fetch("/agent/verification/" + MV_ID + "/party-items")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setData({ loading: false, error: null, parties: d.parties || [] });
        })
        .catch(function (e) {
          setData({ loading: false, error: String(e), parties: [] });
        });
    }

    React.useEffect(reload, []);
    return [data, setData, reload];
  }

  function Field(props) {
    return h("input", {
      value: props.value || "",
      onChange: function (e) { props.onChange(e.target.value); },
      placeholder: props.placeholder || "",
      style: Object.assign({
        border: "none", borderBottom: "1px solid var(--border2)",
        background: "transparent", font: "inherit", fontSize: "12px",
        padding: "3px 4px", width: props.width || "100%", color: "var(--t0)",
      }, props.style || {}),
    });
  }

  function NumField(props) {
    return h("input", {
      type: "number", step: "any",
      value: props.value,
      onChange: function (e) { props.onChange(parseFloat(e.target.value) || 0); },
      style: {
        border: "none", borderBottom: "1px solid var(--border2)",
        background: "transparent", font: "inherit", fontSize: "12px",
        padding: "3px 4px", width: "100%", textAlign: "right", color: "var(--t0)",
      },
    });
  }

  function ItemRow(props) {
    var it = props.item;
    return h("tr", { key: it._key },
      h("td", { style: { padding: "4px 6px" } }, h(Field, { value: it.brand, onChange: function (v) { props.onChange("brand", v); } })),
      h("td", { style: { padding: "4px 6px" } }, h(Field, { value: it.pack, onChange: function (v) { props.onChange("pack", v); } })),
      h("td", { style: { padding: "4px 6px", width: 70 } }, h(NumField, { value: it.quantity, onChange: function (v) { props.onChange("quantity", v); } })),
      h("td", { style: { padding: "4px 6px", width: 80 } }, h(NumField, { value: it.unit_rate, onChange: function (v) { props.onChange("unit_rate", v); } })),
      h("td", { style: { padding: "4px 6px", width: 90 } }, h(NumField, { value: it.final_amount, onChange: function (v) { props.onChange("final_amount", v); } })),
      h("td", { style: { padding: "4px 6px", width: 30, textAlign: "center" } },
        h("button", {
          onClick: props.onDelete, title: "Remove item",
          style: { border: "none", background: "none", color: "var(--red)", cursor: "pointer", fontSize: "13px" },
        }, "×")
      )
    );
  }

  function PartyCard(props) {
    var p = props.entry.party, items = props.entry.items;
    return h("div", {
      style: {
        background: "var(--bg1)", border: "1px solid var(--border)",
        borderRadius: "var(--r12)", padding: "14px 16px", marginBottom: "14px",
      },
    },
      h("div", { style: { display: "flex", gap: "10px", marginBottom: "8px", alignItems: "center" } },
        h(Field, { value: p.name, onChange: function (v) { props.onPartyChange("name", v); }, width: "260px", placeholder: "Party name" }),
        h(Field, { value: p.area, onChange: function (v) { props.onPartyChange("area", v); }, width: "160px", placeholder: "Area" }),
        h("span", { style: { marginLeft: "auto", fontSize: "11px", color: "var(--t2)", fontFamily: "var(--mono)" } },
          items.length + " items"
        ),
        h("button", {
          onClick: props.onDeleteParty, title: "Remove party",
          style: { border: "1px solid var(--red)", background: "none", color: "var(--red)", borderRadius: "6px", cursor: "pointer", padding: "3px 8px", fontSize: "11px" },
        }, "Remove party")
      ),
      h("table", { style: { width: "100%", borderCollapse: "collapse" } },
        h("thead", null,
          h("tr", { style: { fontSize: "10px", color: "var(--t2)", textTransform: "uppercase", textAlign: "left" } },
            h("th", { style: { padding: "0 6px 4px" } }, "Brand"),
            h("th", { style: { padding: "0 6px 4px" } }, "Pack"),
            h("th", { style: { padding: "0 6px 4px" } }, "Qty"),
            h("th", { style: { padding: "0 6px 4px" } }, "Rate"),
            h("th", { style: { padding: "0 6px 4px" } }, "Amount"),
            h("th", null)
          )
        ),
        h("tbody", null,
          items.map(function (it, idx) {
            return h(ItemRow, {
              key: it._key, item: it,
              onChange: function (field, val) { props.onItemChange(idx, field, val); },
              onDelete: function () { props.onItemDelete(idx); },
            });
          })
        )
      ),
      h("button", {
        onClick: props.onAddItem,
        style: {
          marginTop: "8px", border: "1px dashed var(--border2)", background: "none",
          color: "var(--blue)", borderRadius: "6px", cursor: "pointer", padding: "5px 10px", fontSize: "11px",
        },
      }, "+ Add item")
    );
  }

  var _keyCounter = 0;
  function withKeys(parties) {
    return parties.map(function (entry) {
      return {
        party: entry.party,
        items: entry.items.map(function (it) {
          return Object.assign({ _key: "k" + (_keyCounter++) }, it);
        }),
      };
    });
  }

  function App() {
    var pd = useParties();
    var data = pd[0], setData = pd[1], reload = pd[2];
    var dirtyState = React.useState(false);
    var dirty = dirtyState[0], setDirty = dirtyState[1];
    var busyState = React.useState(null); // 'saving' | 'rejecting' | 'reextracting' | null
    var busy = busyState[0], setBusy = busyState[1];
    var msgState = React.useState(null);
    var msg = msgState[0], setMsg = msgState[1];

    React.useEffect(function () {
      if (!data.loading) {
        setData(function (d) { return Object.assign({}, d, { parties: withKeys(d.parties) }); });
      }
      // eslint-disable-next-line
    }, [data.loading]);

    function mutate(fn) {
      setData(function (d) { return Object.assign({}, d, { parties: fn(d.parties) }); });
      setDirty(true);
    }

    function onPartyChange(pIdx, field, val) {
      mutate(function (parties) {
        var copy = parties.slice();
        copy[pIdx] = Object.assign({}, copy[pIdx], { party: Object.assign({}, copy[pIdx].party) });
        copy[pIdx].party[field] = val;
        return copy;
      });
    }
    function onItemChange(pIdx, iIdx, field, val) {
      mutate(function (parties) {
        var copy = parties.slice();
        var items = copy[pIdx].items.slice();
        items[iIdx] = Object.assign({}, items[iIdx]);
        items[iIdx][field] = val;
        copy[pIdx] = Object.assign({}, copy[pIdx], { items: items });
        return copy;
      });
    }
    function onItemDelete(pIdx, iIdx) {
      mutate(function (parties) {
        var copy = parties.slice();
        var items = copy[pIdx].items.slice();
        items.splice(iIdx, 1);
        copy[pIdx] = Object.assign({}, copy[pIdx], { items: items });
        return copy;
      });
    }
    function onAddItem(pIdx) {
      mutate(function (parties) {
        var copy = parties.slice();
        var items = copy[pIdx].items.slice();
        items.push({ _key: "k" + (_keyCounter++), id: 0, brand: "", pack: "", quantity: 0, unit_rate: 0, final_amount: 0 });
        copy[pIdx] = Object.assign({}, copy[pIdx], { items: items });
        return copy;
      });
    }
    function onDeleteParty(pIdx) {
      mutate(function (parties) {
        var copy = parties.slice();
        copy.splice(pIdx, 1);
        return copy;
      });
    }

    function buildPayload() {
      var parties = [], items = [];
      data.parties.forEach(function (entry, pIdx) {
        var pid = entry.party.id || 0;
        var tempId = pid > 0 ? null : "t" + pIdx;
        parties.push({
          id: pid, temp_id: tempId,
          name: entry.party.name || "", area: entry.party.area || "",
          type: entry.party.type || "chemist",
        });
        entry.items.forEach(function (it) {
          items.push({
            id: it.id || 0, party_id: pid > 0 ? pid : tempId,
            brand: it.brand || "", mfg: it.mfg || "", pack: it.pack || "",
            quantity: it.quantity || 0, unit_rate: it.unit_rate || 0,
            discount_percent: it.discount_percent || 0, final_amount: it.final_amount || 0,
          });
        });
      });
      return { parties: parties, items: items, deleted_party_ids: [], deleted_item_ids: [] };
    }

    function save() {
      setBusy("saving"); setMsg(null);
      fetch("/agent/verification/" + MV_ID + "/inline-save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload()),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setBusy(null);
          if (d.success) {
            setDirty(false);
            setMsg({ type: "ok", text: "Saved & verified. Returning to queue…" });
            setTimeout(function () { window.location.href = "/agent/dashboard"; }, 1200);
          } else {
            setMsg({ type: "error", text: d.error || "Save failed" });
          }
        })
        .catch(function (e) { setBusy(null); setMsg({ type: "error", text: "Network error: " + e }); });
    }

    function rejectTask() {
      var reason = window.prompt("Reason for rejecting this task?");
      if (!reason) return;
      setBusy("rejecting"); setMsg(null);
      fetch("/agent/verification/" + MV_ID + "/reject", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: reason }),
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setBusy(null);
          if (d.success) { window.location.href = "/agent/dashboard"; }
          else { setMsg({ type: "error", text: d.error || "Reject failed" }); }
        })
        .catch(function (e) { setBusy(null); setMsg({ type: "error", text: "Network error: " + e }); });
    }

    function reextract() {
      if (!window.confirm("Re-run AI extraction on the original document?\n\nThis DISCARDS all current parties/items (including anything edited here) and replaces them with a fresh extraction. This cannot be undone.")) return;
      setBusy("reextracting"); setMsg(null);
      fetch("/agent/verification/" + MV_ID + "/reextract", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          setBusy(null);
          if (d.success) { setDirty(false); reload(); setMsg({ type: "ok", text: "Re-extracted with AI -- please review." }); }
          else { setMsg({ type: "error", text: d.error || "Re-extraction failed" }); }
        })
        .catch(function (e) { setBusy(null); setMsg({ type: "error", text: "Network error: " + e }); });
    }

    if (data.loading) {
      return h("div", { style: { padding: "60px", textAlign: "center", color: "var(--t2)" } }, "Loading editor…");
    }
    if (data.error) {
      return h("div", { style: { padding: "60px", textAlign: "center", color: "var(--red)" } }, "Failed to load: " + data.error);
    }

    return h(React.Fragment, null,
      h("div", { style: { display: "flex", alignItems: "center", marginBottom: "16px" } },
        h("a", { href: "/agent/dashboard", style: { fontSize: "11px", color: "var(--t2)", textDecoration: "none", marginRight: "12px" } }, "← My Queue"),
        h("div", null,
          h("div", { style: { fontSize: "18px", fontWeight: 700 } }, "Task #" + MV_ID + " · " + (MV.company_name || "")),
          h("div", { style: { fontSize: "11px", color: "var(--t2)", fontFamily: "var(--mono)" } },
            (MV.original_filename || "") + " — React preview editor"
          )
        ),
        h("a", {
          href: "/agent/document/" + MV_ID, style: {
            marginLeft: "auto", fontSize: "11px", color: "var(--blue)", border: "1px solid var(--blue)",
            borderRadius: "6px", padding: "6px 10px", textDecoration: "none",
          },
        }, "Switch to classic editor")
      ),
      data.parties.map(function (entry, pIdx) {
        return h(PartyCard, {
          key: entry.party.id || ("new" + pIdx),
          entry: entry,
          onPartyChange: function (field, val) { onPartyChange(pIdx, field, val); },
          onItemChange: function (iIdx, field, val) { onItemChange(pIdx, iIdx, field, val); },
          onItemDelete: function (iIdx) { onItemDelete(pIdx, iIdx); },
          onAddItem: function () { onAddItem(pIdx); },
          onDeleteParty: function () { onDeleteParty(pIdx); },
        });
      }),
      msg ? h("div", {
        style: {
          padding: "10px 14px", borderRadius: "8px", fontSize: "12px", marginBottom: "12px",
          background: msg.type === "ok" ? "var(--green-d)" : "var(--red-d)",
          color: msg.type === "ok" ? "var(--green)" : "var(--red)",
        },
      }, msg.text) : null,
      h("div", {
        style: {
          position: "sticky", bottom: "10px", background: "var(--bg2)", border: "1px solid var(--border2)",
          borderRadius: "var(--r8)", padding: "12px 14px", display: "flex", alignItems: "center",
          justifyContent: "space-between", gap: "10px",
        },
      },
        h("span", { style: { fontSize: "11px", color: dirty ? "var(--amber)" : "var(--t2)" } },
          dirty ? "● Unsaved changes" : "No unsaved changes"
        ),
        h("div", { style: { display: "flex", gap: "8px" } },
          h("button", {
            onClick: reextract, disabled: !!busy,
            style: { padding: "8px 14px", background: "none", border: "1px solid var(--blue)", borderRadius: "8px", color: "var(--blue)", fontSize: "12px", cursor: "pointer" },
          }, busy === "reextracting" ? "Re-extracting…" : "↻ Re-extract with AI"),
          h("button", {
            onClick: rejectTask, disabled: !!busy,
            style: { padding: "8px 14px", background: "none", border: "1px solid var(--red)", borderRadius: "8px", color: "var(--red)", fontSize: "12px", cursor: "pointer" },
          }, busy === "rejecting" ? "Rejecting…" : "✗ Reject"),
          h("button", {
            onClick: save, disabled: !!busy,
            style: { padding: "8px 18px", background: "var(--green)", border: "none", borderRadius: "8px", color: "#fff", fontSize: "12px", fontWeight: 700, cursor: "pointer" },
          }, busy === "saving" ? "Saving…" : "✓ Save & Verify")
        )
      )
    );
  }

  ReactDOM.createRoot(document.getElementById("root")).render(h(App));
})();
