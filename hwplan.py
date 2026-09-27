diff --git a/hwplan.py b/hwplan.py
index bb81c803471d0dbff1dae7a197636ee2a9d7385a..8a6c6c7205280216cbe53fd474c11d606e257fa9 100644
--- a/hwplan.py
+++ b/hwplan.py
@@ -279,58 +279,61 @@ def plan(ctx, reach):
         dn_mx = max((max(pl["per"][k][0]) for k in dn_pts if k in pl["per"]), default=None)
         wait = te - e_j
         J = pl["e"] + float(P["adj_cost"]) * sum(abs(v) for v in cadj.values())
         key = (0 if pl["imb"] <= TOL else 1, pl["imb"] if pl["imb"] > TOL else 0.0, J)
         cands.append({"j": j, "no": j + 1, "code": O["stops"][j]["code"], "name": O["stops"][j]["name"], "lat": O["stops"][j]["lat"], "lon": O["stops"][j]["lon"],
                       "skip": j, "avoided": _r(SM * j), "os_min": _r(rmin), "os_km": _r(rkm, 2), "os_src": rsrc,
                       "ready": hhmm(INS["ready"]), "leave": hhmm(INS["ready"] + wait), "wait": _r(wait), "earliest": hhmm(e_j),
                       "entry": _r(te), "entry_clock": hhmm(te), "net": _r(SM * j - rmin),
                       "gap": {"front": fu["label"], "front_role": fu.get("role"), "rear": U[nx[1]]["label"] if nx[1] else "the next trip (beyond the forecast)",
                               "rear_role": U[nx[1]].get("role") if nx[1] else None, "t_front": hhmm(f[0]), "t_rear": hhmm(nx[0]),
                               "minutes": _r(nx[0] - f[0]), "minutes_raw": _r(pl0["nx"][0] - pl0["f"][0]) if (use_adj and isinstance(pl0, dict)) else None,
                               "hold": 0.0, "mid": hhmm(pl["mid"]), "split": [_r(te - f[0]), _r(nx[0] - te)], "imbalance": _r(pl["imb"]), "even": pl["imb"] <= TOL},
                       "front": {"uid": fu["uid"], "num": fu["num"], "role": fu.get("role"), "label": fu["label"], "pass": hhmm(tp(fu)), "adj": cadj.get(fu["uid"], 0.0),
                                 "dep": hhmm(fu["dep"]), "movable": fu["movable"]},
                       "rear": ({"uid": ru["uid"], "num": ru["num"], "role": ru.get("role"), "label": ru["label"], "pass": hhmm(tp(ru)), "adj": cadj.get(ru["uid"], 0.0),
                                 "dep": hhmm(ru["dep"])} if ru else None),
                       "slows": [], "rear_log": [], "use_adj": use_adj,
                       "dep_adj": [dict(a_, adj=(a_["adj"] if use_adj else 0.0), dep_new=(a_["dep_new"] if use_adj else a_["dep"]),
                                        reason=(a_["reason"] if use_adj else ("not needed for this plan" if a_["adj"] else a_["reason"]))) for a_ in adj_list],
                       "ewt_noadj": round(pl0["e"], 3) if (use_adj and isinstance(pl0, dict)) else None,
                       "ewt_adj": round(pl0["e"], 3) if (not use_adj and dep_adj and isinstance(pl0, dict)) else None,
                       "gap_before": _r(te - tp(fu)) if tp(fu) is not None else None, "gap_after": _r(tp(ru) - te) if ru and tp(ru) is not None else None,
                       "max_hw": _r(dn_mx if dn_mx is not None else pl["mx"]), "max_hw_all": _r(pl["mx"]), "ewt": round(pl["e"], 3), "score": J,
                       "gain": round(e0 - pl["e"], 3), "km_skipped": _r(O["ss"][j], 2), "rkey": key + (j,),
                       "adj": dict(cadj), "_tab": tab, "_adj": dict(cadj)})
-    # RECOMMENDED: the first stop where Bus C can land in the middle of the prolonged headway (fewest stops skipped);
-    # if no stop allows an even split, the most even one. Lowest whole-route EWT is shown alongside.
-    cands.sort(key=lambda x: x["rkey"][:2] + (x["j"],))
+    # RECOMMENDED: lowest forecast downstream EWT. Gap balance remains visible to the
+    # controller and is the first tie-break, but it must not outrank the management
+    # objective. The final tie-break prefers fewer skipped stops.
+    rank_key = lambda x: (x["ewt"], x["max_hw"], x["gap"]["imbalance"],
+                          sum(abs(a["adj"]) for a in x["dep_adj"]), x["j"])
+    cands.sort(key=rank_key)
     for i, x in enumerate(cands, 1):
         x["rank"] = i
     best = cands[0] if cands else None
-    best_ewt = min(cands, key=lambda x: (x["score"], x["j"])) if cands else None
-    er = sorted(cands, key=lambda y: y["score"])
+    best_ewt = best
+    er = sorted(cands, key=rank_key)
     for x in cands:
         x["ewt_rank"] = er.index(x) + 1
 
     def ic_lane(adj):
         out = []
         for r in ("A", "B", "C", "D", "E"):
             if r not in roles:
                 continue
             u = roles[r]
             if adj is not None and r == "C" and not OSM:
                 continue                                          # Bus C does not depart from the interchange - it goes halfway
             d_ = (adj or {}).get(u["uid"], 0.0)
             out.append({"role": r, "num": u["num"], "label": u["label"], "t": _r(u["dep"] + d_), "clock": hhmm(u["dep"] + d_), "adj": d_,
                         "in_service": u["kind"] == "in_service", "late": u["uid"] == C["uid"]})
         return out
 
     def detail(x):
         tab, adj, j = x["_tab"], x["_adj"], x["j"]
         cols = [j] + [k for k in (j + 5, j + 10, j + 15) if k < nO]
         ring = [roles[r] for r in ("A", "B", "C", "D", "E") if r in roles]
         if OSM:
             ring = ring[:ring.index(C) + 1] + [INS] + ring[ring.index(C) + 1:]
         rows = []
         for u in ring:
             t = tab.get(u["uid"], {})
@@ -370,62 +373,64 @@ def plan(ctx, reach):
     def why(x):
         g = x["gap"]
         parts, fixed = adj_sentence(x)
         s = ""
         if OSM:
             s += (f"Bus {Cn} completes {tl} {D:g} min late and runs its {ol} trip from {O['stops'][0]['name']} at {hhmm(full_dep)} (full trip). "
                   f"An extra OS bus with a Bus Captain fills the gap instead. ")
         if parts:
             s += ((f"The interchange headway around the late Bus {Cn} is evened out: " if OSM else
                    f"Bus {Cn}'s departure slot at {O['stops'][0]['name']} is empty, so the interchange headway is closed by spacing the buses around it evenly: ")
                   + "; ".join(parts) + ". ")
         if fixed:
             s += " ".join(fixed) + ". "
         s += (f"At Stop {x['no']} the headway {WHO} has to fill is then between {g['front']} ({g['t_front']}) and {g['rear']} ({g['t_rear']}): {g['minutes']:.0f} min"
               + (f" (was {g['minutes_raw']:.0f})" if g.get("minutes_raw") and abs(g["minutes_raw"] - g["minutes"]) >= 1 else "")
               + f". Half of it puts {WHO} at {g['mid']}, leaving {g['split'][0]:.0f} / {g['split'][1]:.0f} min. ")
         if OSM:
             s += (f"The OS bus is available at {INS['src']} from {x['ready']}; the real road route to Stop {x['no']} (BS {x['code']}) takes about "
                   f"{x['os_min']:.0f} min ({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
         else:
             s += (f"Bus {Cn} completes {tl} at {hhmm(t_buses[c]['arr'])} ({D:g} min late), is ready at {x['ready']} after the {BRK:g}-min break, and the real road route "
                   f"to Stop {x['no']} (BS {x['code']}) takes about {x['os_min']:.0f} min ({x['os_km']:.1f} km) - earliest arrival {x['earliest']}. ")
         if x["wait"] and x["wait"] >= 0.5:
             s += f"It therefore leaves {'its start point' if OSM else 'the interchange'} {x['wait']:.0f} min later ({x['leave']}) so it enters exactly mid-gap at {x['entry_clock']}. "
         if g["even"]:
-            s += f"Stop {x['no']} is the first stop where {WHO} can reach the middle of the gap, so it skips the fewest stops ({x['skip']}). "
+            s += f"The bus can physically reach the projected midpoint, producing a balanced {g['split'][0]:.0f} / {g['split'][1]:.0f}-min insertion. "
         else:
-            s += f"No stop lets {WHO} reach the middle in time; Stop {x['no']} gives the most even split. "
+            s += f"The earliest feasible arrival produces a {g['split'][0]:.0f} / {g['split'][1]:.0f}-min split. "
         s += f"Downstream {ol} EWT {x['ewt']:.3f} min (no action {e0:.3f}"
         if x.get("ewt_noadj") is not None and parts:
             s += f"; {x['ewt_noadj']:.3f} with the halfway alone, without the interchange adjustments"
         if x.get("ewt_adj") is not None:
             s += f"; moving the other buses at the interchange would give {x['ewt_adj']:.3f}, so they depart as planned"
         s += f"); largest headway after entry {x['max_hw']:.0f} min."
-        if best_ewt and best_ewt["code"] != x["code"]:
-            s += (f" Stop {best_ewt['no']} has the lowest whole-route EWT ({best_ewt['ewt']:.3f}) because {WHO} serves more stops there, "
-                  f"but it splits the gap {best_ewt['gap']['split'][0]:.0f} / {best_ewt['gap']['split'][1]:.0f} min.")
+        if best and best["code"] == x["code"]:
+            s += " This is the AI recommendation because it has the lowest forecast downstream EWT of every feasible entry point tested."
+        elif best:
+            s += (f" Stop {best['no']} is recommended instead because its downstream EWT is lower ({best['ewt']:.3f}), "
+                  f"with a {best['gap']['split'][0]:.0f} / {best['gap']['split'][1]:.0f}-min insertion.")
         return s
 
     def actions(x):
         steps, n = [], 1
         for a_ in [a for a in x["dep_adj"] if a["role"] in ("A", "B")]:
             if a_["adj"] > 0:
                 steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "hold", "text": f"Depart the interchange {a_['adj']:.0f} min later ({a_['dep']} \u2192 {a_['dep_new']}) to close the headway gap.", "time": a_["dep_new"]})
             else:
                 steps.append({"n": n, "bus": a_["label"], "role": a_["role"], "kind": "monitor", "text": f"Depart as planned ({a_['dep']}) \u2013 {a_['reason'] or 'no change needed'}.", "time": a_["dep"]})
             n += 1
         if OSM:
             steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "monitor",
                           "text": f"Complete {tl} ({D:g} min late). Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}, break, then run the full {ol} trip from {hhmm(full_dep)}.",
                           "time": hhmm(full_dep)}); n += 1
             steps.append({"n": n, "bus": "OS bus + Bus Captain", "role": "OS", "kind": "halfway",
                           "text": f"Deploy from {INS['src']}. Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive and enter {ol} at {x['entry_clock']} "
                                   f"({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}). Continue to the final stop.", "time": x["entry_clock"]}); n += 1
         wtxt = f" Extend the break by {x['wait']:.0f} min" if x["wait"] and x["wait"] >= 0.5 else ""
         if not OSM:
           steps.append({"n": n, "bus": f"{tl} Bus {Cn}", "role": "C", "kind": "halfway",
                       "text": f"Complete {tl}. Arrive {ol} interchange {hhmm(t_buses[c]['arr'])}. Break until {x['ready']}.{wtxt}"
                               f"{'.' if wtxt else ''} Depart off-service {x['leave']} to Stop {x['no']} (BS {x['code']} {x['name']}), arrive and enter {ol} at {x['entry_clock']} "
                               f"({x['gap']['split'][0]:.0f} min behind {x['gap']['front']}). Continue to the final stop.",
                       "time": x["entry_clock"]}); n += 1
         for a_ in [a for a in x["dep_adj"] if a["role"] in ("D", "E")]:
