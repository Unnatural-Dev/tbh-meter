import { useEffect, useMemo, useState } from "react";
import { Package, Pencil, Trash2, TrendingUp } from "lucide-react";
import type { AppSettings } from "../../../shared/ipc-types.js";
import {
  buildChestPlan, recordedChestBonuses, isPlagueStage, MAX_CHEST_BONUSES, parseClearTime, plannerStages, plannerBoxes,
  type ChestPlannerSettings, type ChestDropBonuses,
} from "../../../shared/chest-planner.js";
import { EMPTY_CHEST_AUTO, type ChestPlannerAutoData } from "../../../shared/chest-planner-inputs.js";
import { useI18n } from "~/lib/i18n";
import { modeLabel } from "~/lib/format";
import { cn } from "~/lib/utils";

interface Props {
  settings: AppSettings;
  onSettingsChange: (partial: Partial<AppSettings>) => void;
}

const inputClass = "rounded border border-surface-600 bg-surface-900 px-2 py-1.5 text-xs text-zinc-200 outline-none focus:border-brand-400";
const buttonClass = "cursor-pointer rounded bg-surface-700 px-2.5 py-1.5 text-xs text-zinc-200 hover:bg-surface-600 focus-visible:outline-2 focus-visible:outline-brand-400";

export function ChestPlannerView({ settings, onSettingsChange }: Props) {
  const { t, lang } = useI18n();
  const planner = settings.chestPlanner;
  const [search, setSearch] = useState("");
  const [stageKey, setStageKey] = useState(plannerStages[0]?.key ?? 0);
  const [clear, setClear] = useState("");
  const [error, setError] = useState(false);
  const [auto, setAuto] = useState<ChestPlannerAutoData>(EMPTY_CHEST_AUTO);
  const [loadFailed, setLoadFailed] = useState(false);
  useEffect(() => {
    let disposed = false;
    let request = 0;
    const load = async () => {
      const current = ++request;
      try {
        const data = await window.meter.getChestPlannerInputs();
        if (!disposed && current === request) { setAuto(data); setLoadFailed(false); }
      } catch {
        if (!disposed && current === request) { setAuto(EMPTY_CHEST_AUTO); setLoadFailed(true); }
      }
    };
    void load();
    const unsubscribe = window.meter.onRunsChanged(() => void load());
    return () => { disposed = true; unsubscribe(); };
  }, [settings.outputDir]);
  const groups = useMemo(() => buildChestPlan(planner, auto), [planner, auto]);
  const displayBonuses = planner.bonusMode === "maxed" ? MAX_CHEST_BONUSES
    : planner.bonusMode === "auto" ? recordedChestBonuses(auto) : planner.bonuses;
  const labelStage = (stage: typeof plannerStages[number]) =>
    `${isPlagueStage(stage) ? `${t("chestPlanner.plaguelands")} ${stage.stageNo}` : `${stage.act}-${stage.stageNo}`} ${modeLabel(stage.difficulty.slice(0, 1) + stage.difficulty.slice(1).toLowerCase(), t)}`;
  const matching = plannerStages.filter((stage) => `${labelStage(stage)} ${stage.name}`.toLowerCase().includes(search.trim().toLowerCase()));
  const selected = matching.some((stage) => stage.key === stageKey) ? stageKey : matching[0]?.key ?? 0;

  const save = (next: ChestPlannerSettings) => onSettingsChange({ chestPlanner: next });
  const saveTime = (event: React.FormEvent) => {
    event.preventDefault();
    const seconds = parseClearTime(clear);
    if (seconds == null || !selected) { setError(true); return; }
    save({ ...planner, clearTimes: { ...planner.clearTimes, [selected]: seconds }, excludedStages: planner.excludedStages?.filter((key) => key !== selected) });
    setClear("");
    setError(false);
  };
  const editTime = (key: number, seconds: number) => {
    setSearch("");
    setStageKey(key);
    setClear(String(seconds));
    setError(false);
    document.getElementById("chest-clear-time")?.focus();
  };
  const removeTime = (key: number) => {
    const clearTimes = { ...planner.clearTimes };
    delete clearTimes[key];
    const excludedStages = planner.clearTimes[key] != null && auto.clearTimes[key] != null
      ? planner.excludedStages?.filter((stage) => stage !== key)
      : [...new Set([...(planner.excludedStages ?? []), key])];
    save({ ...planner, clearTimes, excludedStages });
  };
  const bonusFields = [
    ["generalBossPercent", "chestPlanner.bossBonus"],
    ["generalNormalPercent", "chestPlanner.monsterBonus"],
    ["plagueBossPercent", "chestPlanner.plagueBossBonus"],
    ["plagueNormalPercent", "chestPlanner.plagueMonsterBonus"],
  ] as const;
  const updateBonus = (key: keyof ChestDropBonuses, value: string) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return;
    save({ ...planner, bonuses: { ...planner.bonuses, [key]: Math.max(0, Math.min(10000, n)) } });
  };
  const oddsToggle = () => (
    <div className="flex gap-1" role="group" aria-label={t("chestPlanner.bonuses")}>
      {(["auto", "custom", "maxed"] as const).map((mode) => (
        <button key={mode} type="button" aria-pressed={planner.bonusMode === mode}
          onClick={() => save({ ...planner, bonusMode: mode })}
          className={cn(buttonClass, "py-1 text-[11px]", planner.bonusMode === mode && "bg-brand-600/30 text-brand-300 ring-1 ring-brand-500/50")}>
          {t(mode === "auto" ? "chestPlanner.autoOdds" : mode === "custom" ? "chestPlanner.yourOdds" : "chestPlanner.maxedOdds")}
        </button>
      ))}
    </div>
  );

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
      <header>
        <h1 className="flex items-center gap-2 text-sm font-semibold text-zinc-100"><Package className="size-4 text-sky-300" />{t("chestPlanner.title")}</h1>
        <p className="mt-1 max-w-2xl text-xs leading-relaxed text-zinc-400">{t("chestPlanner.description")}</p>
      </header>

      <form onSubmit={saveTime} className="rounded-lg border border-surface-600 bg-surface-800/50 p-3">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-32 flex-1 flex-col gap-1 text-[11px] text-zinc-400">
            {t("chestPlanner.search")}
            <input value={search} onChange={(e) => setSearch(e.target.value)} className={inputClass} />
          </label>
          <label className="flex min-w-40 flex-1 flex-col gap-1 text-[11px] text-zinc-400">
            {t("chestPlanner.stage")}
            <select value={selected} onChange={(e) => setStageKey(Number(e.target.value))} className={inputClass}>
              {matching.length ? matching.map((stage) => <option key={stage.key} value={stage.key}>{labelStage(stage)}</option>) : <option value={0}>{t("chestPlanner.noMatches")}</option>}
            </select>
          </label>
          <label className="flex w-28 flex-col gap-1 text-[11px] text-zinc-400" htmlFor="chest-clear-time">
            {t("chestPlanner.clearTime")}
            <input id="chest-clear-time" value={clear} onChange={(e) => { setClear(e.target.value); setError(false); }}
              placeholder="0:58" aria-invalid={error} aria-describedby={error ? "chest-time-error" : "chest-time-hint"} className={inputClass} />
          </label>
          <button type="submit" disabled={!selected} className={cn(buttonClass, "bg-brand-600 text-white hover:bg-brand-500 disabled:opacity-40")}>{t("chestPlanner.add")}</button>
        </div>
        <p id="chest-time-hint" className="mt-2 text-[11px] text-zinc-500">{t("chestPlanner.clearHint")} · {t("chestPlanner.saved")}</p>
        {error && <p id="chest-time-error" role="alert" className="mt-1 text-xs text-red-300">{t("chestPlanner.invalidTime")}</p>}
      </form>

      <section className="rounded-lg border border-surface-600 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-xs font-medium text-zinc-300">{t("chestPlanner.bonuses")}</h2>
          {oddsToggle()}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3">
          {bonusFields.map(([key, label]) => <label key={key} className="flex flex-col gap-1 text-[11px] text-zinc-400">
            {t(label)}<input type="number" min={0} max={10000} step="any" value={displayBonuses[key]} disabled={planner.bonusMode !== "custom"} onChange={(e) => updateBonus(key, e.target.value)} className={cn(inputClass, "disabled:opacity-60")} />
          </label>)}
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">{t("chestPlanner.bonusHint")}</p>
        {planner.bonusMode === "auto" && <p className="mt-2 text-[11px] leading-relaxed text-amber-300">{t(auto.runeBonuses == null ? "chestPlanner.runesUnavailable" : "chestPlanner.runesLoaded")} {t(auto.petsCaptured ? "chestPlanner.petsLoaded" : "chestPlanner.petsUnavailable")}</p>}
        {auto.runeSnapshotTs != null && <p className="mt-1 text-[10px] text-zinc-500">{t("chestPlanner.snapshotDate", { date: new Date(auto.runeSnapshotTs).toLocaleString(lang) })}</p>}
        {auto.petSnapshotTs != null && <p className="mt-1 text-[10px] text-zinc-500">{t("chestPlanner.petsSnapshotDate", { date: new Date(auto.petSnapshotTs).toLocaleString(lang) })}</p>}
        {planner.bonusMode === "custom" && (auto.runeBonuses != null || auto.petBonuses != null) && <button type="button" className={cn(buttonClass, "mt-2")} onClick={() => save({ ...planner, bonuses: recordedChestBonuses(auto) })}>{t("chestPlanner.copyRunes")}</button>}
        {loadFailed && <p role="alert" className="mt-2 text-xs text-red-300">{t("chestPlanner.loadFailed")}</p>}
      </section>

      <p className="text-[11px] text-zinc-500">{t("chestPlanner.timedOnly")} {t("chestPlanner.estimate")}</p>
      {!groups.length && <div className="rounded-lg border border-dashed border-surface-600 px-4 py-10 text-center text-xs text-zinc-400">{t("chestPlanner.empty")}</div>}
      {groups.map((group) => {
        const box = plannerBoxes.get(group.boxKey);
        const best = group.rows[0];
        return <section key={group.boxKey} className="rounded-lg border border-surface-600 bg-surface-800/40">
          <div className="flex flex-wrap items-center gap-2 border-b border-surface-600 px-3 py-2.5">
            <Package className="size-6 text-sky-300" />
            <div className="min-w-0 flex-1"><h2 className="text-xs font-semibold text-sky-200">{box?.name ?? group.boxKey}</h2>
              <p className="mt-0.5 text-[10px] text-zinc-500">{t("chestPlanner.compare", { count: group.rows.length })}</p></div>
            {oddsToggle()}
          </div>
          <div className="px-3 py-3">
            <div className="flex items-center justify-between gap-3">
              <div><p className="text-[10px] uppercase tracking-wide text-zinc-500">{t("chestPlanner.best")}</p>
                <p className="mt-1 text-sm font-semibold text-zinc-100">{labelStage(best.stage)} <span className="text-xs font-normal text-zinc-400">· {Number(best.clearSeconds.toFixed(2))}s</span></p></div>
              <div className="text-right"><span className="font-mono text-xl font-semibold tabular-nums text-emerald-300">{best.rates.totalPerHour.toFixed(2)}</span><p className="text-[10px] text-zinc-500">{t("chestPlanner.totalHour")}</p></div>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[11px]">
              <thead className="border-y border-surface-600 bg-surface-900/40 text-[10px] text-zinc-500"><tr>
                <th className="px-3 py-2 font-medium">{t("chestPlanner.stage")}</th><th className="px-2 py-2 font-medium">{t("chestPlanner.clearTime")}</th>
                <th className="px-2 py-2 text-right font-medium">{t("chestPlanner.bossHour")}</th><th className="px-2 py-2 text-right font-medium">{t("chestPlanner.monsterHour")}</th>
                <th className="px-2 py-2 text-right font-medium">{t("chestPlanner.totalHour")}</th><th><span className="sr-only">{t("chestPlanner.edit")}</span></th>
              </tr></thead>
              <tbody>{group.rows.map((row) => <tr key={row.stage.key} className="border-b border-surface-700/60 align-top last:border-0">
                <td className="px-3 py-2"><div className="whitespace-nowrap text-zinc-200">{labelStage(row.stage)}</div><p className="mt-0.5 text-[10px] text-zinc-500">{planner.clearTimes[row.stage.key] != null ? t("chestPlanner.manualTime") : t("chestPlanner.recordedTime", { count: auto.sampleCounts[row.stage.key] ?? 0 })}</p>
                  {row.rates.rateLimited && <span className="mt-1 block text-[10px] text-amber-300">{t("chestPlanner.rateLimited")}</span>}
                  {row.advice && <p className="mt-1 flex max-w-56 gap-1 text-[10px] leading-relaxed text-emerald-300/90"><TrendingUp className="mt-0.5 size-3 shrink-0" />{t("chestPlanner.suggestion", { target: row.advice.targetSeconds, seconds: Number(row.advice.secondsToSave.toFixed(2)), gain: row.advice.gainPerHour.toFixed(2) })}</p>}
                </td>
                <td className="px-2 py-2 font-mono text-zinc-300">{Number(row.clearSeconds.toFixed(2))}s</td>
                <td className="px-2 py-2 text-right font-mono tabular-nums text-zinc-400">{row.rates.bossPerHour.toFixed(2)}</td>
                <td className="px-2 py-2 text-right font-mono tabular-nums text-zinc-400">{row.rates.monsterPerHour.toFixed(2)}</td>
                <td className="px-2 py-2 text-right font-mono tabular-nums text-emerald-300">{row.rates.totalPerHour.toFixed(2)}</td>
                <td className="whitespace-nowrap px-2 py-2 text-zinc-500"><button type="button" title={t("chestPlanner.edit")} aria-label={`${t("chestPlanner.edit")} ${labelStage(row.stage)}`} onClick={() => editTime(row.stage.key, row.clearSeconds)} className="cursor-pointer rounded p-1 hover:text-white"><Pencil className="size-3" /></button><button type="button" title={t(planner.clearTimes[row.stage.key] != null && auto.clearTimes[row.stage.key] != null ? "chestPlanner.useRecorded" : "chestPlanner.remove")} aria-label={`${t(planner.clearTimes[row.stage.key] != null && auto.clearTimes[row.stage.key] != null ? "chestPlanner.useRecorded" : "chestPlanner.remove")} ${labelStage(row.stage)}`} onClick={() => removeTime(row.stage.key)} className="cursor-pointer rounded p-1 hover:text-red-300"><Trash2 className="size-3" /></button></td>
              </tr>)}</tbody>
            </table>
          </div>
        </section>;
      })}
      <footer className="pb-2 text-[10px] text-zinc-500">{t("chestPlanner.source")}</footer>
    </div>
  );
}
