// Extract factual planner inputs from a saved TBH Index asset snapshot, without executing
// downloaded JavaScript. The app bundles the result and never contacts the website.
// Usage: node scripts/refresh-chest-planner.mjs --assets <saved-assets-directory>
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const assets = process.argv[process.argv.indexOf("--assets") + 1];
if (!process.argv.includes("--assets") || !assets) {
  throw new Error("Usage: refresh-chest-planner.mjs --assets <saved-assets-directory>");
}
const files = readdirSync(assets);
function source(prefix) {
  const matches = files.filter((f) => f.startsWith(prefix) && f.endsWith(".js"));
  if (matches.length !== 1) throw new Error(`Expected one ${prefix} asset; found ${matches.length}`);
  return { name: matches[0], text: readFileSync(join(assets, matches[0]), "utf8").replace(/^\uFEFF/, "") };
}
function embeddedJson(text) {
  const literal = text.match(/JSON\.parse\(`([^`]+)`\)/)?.[1];
  if (!literal) throw new Error("Expected an embedded JSON snapshot");
  return JSON.parse(literal);
}
const chestSource = source("tbhChestNames-");
const stageSource = source("tbhStages-");
const runeSource = source("statsReference-");
const petSource = source("tbhPets-");
const boxes = {};
const boxPattern = /(\d+):\{name:`([^`]+)`,grade:`([^`]+)`,tier:`([^`]+)`,tradable:![01],cooldown:([\d.]+)\}/g;
for (const [, key, name, grade, tier, minutes] of chestSource.text.matchAll(boxPattern)) {
  boxes[key] = { name, grade, tier, cooldownSeconds: Number(minutes) * 60 };
}
const stages = embeddedJson(stageSource.text).stages
  .filter((stage) => boxes[stage.bossBox]?.cooldownSeconds > 0)
  .map((stage) => ({
    key: stage.key, act: stage.act, stageNo: stage.no, difficulty: stage.diff,
    level: stage.level, name: stage.name?.en ?? "", boxKey: stage.bossBox,
    monsterBoxKey: boxes[stage.monsterBox]?.cooldownSeconds > 0 ? stage.monsterBox : null,
    bossRate: stage.bossBoxRate ?? 1000, monsterRate: stage.monsterBoxRate ?? 0,
    monsters: (stage.waves ?? 0) * (stage.waveMonsters ?? 0),
  }));
const statFields = {
  DropChanceStageBossChestPercent: "generalBossPercent",
  DropChanceNormalChestPercent: "generalNormalPercent",
  DropChancePlagueStageBossChestPercent: "plagueBossPercent",
  DropChancePlagueNormalChestPercent: "plagueNormalPercent",
};
const maxedBonuses = Object.fromEntries(Object.values(statFields).map((field) => [field, 0]));
const runeBonuses = {};
for (const match of runeSource.text.matchAll(/(\d+):\{maxLevel:(\d+),levels:\[([^\]]*)\]\}/g)) {
  const levels = [...match[3].matchAll(/statType:`([^`]+)`,statValue:`([^`]+)`/g)];
  if (!statFields[levels[0]?.[1]]) continue;
  const effects = levels.slice(0, Number(match[2])).map(([, stat, value]) => ({
    field: statFields[stat], percent: Number(value) / 10,
  }));
  for (const effect of effects) {
    if (effect.field) maxedBonuses[effect.field] += effect.percent;
  }
  runeBonuses[match[1]] = effects;
}
const petBonuses = {};
for (const pet of embeddedJson(petSource.text)) {
  if (!Number.isInteger(pet.key) || petBonuses[pet.key] || !Array.isArray(pet.bonuses)) {
    throw new Error("Unsupported pet catalog row");
  }
  const effects = [];
  for (const bonus of pet.bonuses) {
    const field = statFields[bonus.stat];
    if (field) {
      if (bonus.mod !== "FLAT" || !Number.isFinite(bonus.value) || bonus.value < 0) {
        throw new Error(`Unsupported chest bonus for pet ${pet.key}`);
      }
      maxedBonuses[field] += bonus.value / 10;
      effects.push({ field, percent: bonus.value / 10 });
    }
  }
  petBonuses[pet.key] = effects;
}
if (stages.length === 0 || Object.keys(boxes).length === 0 || Object.keys(petBonuses).length === 0 || maxedBonuses.generalBossPercent <= 0) {
  throw new Error("Incomplete planner snapshot; review changed source asset formats");
}
const snapshot = {
  source: "https://tbhindex.com/wiki/chest-tracker",
  assets: [chestSource.name, stageSource.name, runeSource.name, petSource.name],
  // These are the website's empirical model assumptions, not live game readiness timers.
  transitionSeconds: 2.45, plagueCombinedCapPerHour: 24.3,
  maxedBonuses, runeBonuses, petBonuses, boxes, stages,
};
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
writeFileSync(join(root, "data/json/chest-planner.json"), `${JSON.stringify(snapshot, null, 2)}\n`);
console.log(`Planner snapshot: ${stages.length} stages, ${Object.keys(boxes).length} boxes`);
