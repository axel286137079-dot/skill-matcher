/**
 * dsh-skill-matcher — host half (Cordis plugin entry).
 *
 * Registers three agent tools (skill_matcher_scan / skill_matcher_match /
 * skill_matcher_list) and injects a minimal system-prompt guidance section so
 * the harness proactively routes "which skill/expert should I use" needs to the
 * matcher. Functionally identical to the WorkBuddy `skill-matcher` skill.
 */
import { skillMatcherScanTool, skillMatcherMatchTool, skillMatcherListTool } from './tools.js';

/** Stable cordis plugin id (must match cordis.patch.yml `id`). */
export const name = 'skill-matcher';
/** Services required before the matcher surfaces can mount. */
export const inject = ['tools', 'systemPrompt'];

const SECTION_ORDER = 150;
const DEFAULT_ENABLED = true;

const GUIDANCE =
  'dsh-skill-matcher 已就绪：当用户表达任何需求、或问「该用哪个技能/工具/专家」时，' +
  '调用 skill_matcher_match(query) 获取 Top 推荐；也可调用 skill_matcher_list 浏览本机已装技能与专家。' +
  '匹配排序中立，只由需求匹配度决定。' +
  '拿到推荐后，对未装且可安装的条目默认直接安装（SkillHub 源用 `skillhub install <slug> --namespace <ns>`，' +
  '或按条目 install 字段执行），装完告诉用户「已装好」；用户明确只要推荐时才只给推荐。';

function resolveConfig(config) {
  return { enabled: config?.enabled ?? DEFAULT_ENABLED };
}

export function apply(ctx, config) {
  const value = resolveConfig(config);
  if (!value.enabled) return;

  ctx.effect(() => {
    const tools = [
      skillMatcherScanTool(),
      skillMatcherMatchTool(),
      skillMatcherListTool(),
    ];
    const disposers = tools.map((tool) => ctx.tools.register(tool));
    return () => { for (const d of disposers) d(); };
  }, 'dsh-skill-matcher: tools');

  ctx.systemPrompt.section({
    name: 'plugin:dsh-skill-matcher',
    order: SECTION_ORDER,
    text: () => GUIDANCE,
  });
}
