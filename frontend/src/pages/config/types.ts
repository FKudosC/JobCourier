/* ------------------------------------------------------------------ */
/* 类型                                                                */
/* ------------------------------------------------------------------ */

export interface Pace {
  label: string;
  params: Record<string, number>;
  custom: boolean;
  hint: string;
}

export interface Options {
  cities: string[];
  job_type: string[];
  salary: string[];
  experience: string[];
  education: string[];
  funding: string[];
  scale: string[];
  pace: string[];
}

export interface Tailor {
  enabled: boolean;
  min_score: number;
}

export interface Llm {
  base_url: string;
  api_key: string;
  /** 后端是否已保存 Key（api_key 只回掩码，界面上不展示明文）。 */
  key_set: boolean;
  model: string;
  models: string[];
  ready: boolean;
}

/** 验证码识别：开关 + 识图模型（留空用大模型服务里的主模型）。 */
export interface Captcha {
  enabled: boolean;
  model: string;
}

/** 表单定位兜底：认不出的栏位截图交给多模态模型指认；默认关闭。 */
export interface Locate {
  enabled: boolean;
  /** 一次扫描最多处理几个栏位。 */
  max_fields: number;
  /** 每个栏位的超时（秒）。 */
  timeout: number;
}

/** 表单视觉核对：把扫出来的栏位编号画在截图上，请多模态模型核对多没多少没少。 */
export interface Vision {
  enabled: boolean;
  /** 核对用的识图模型；留空则用大模型服务里选中的那个。 */
  model: string;
  /** 一次核对的总超时（秒）。 */
  timeout: number;
  /** 长表单最多切几屏给模型看。 */
  max_tiles: number;
  /** 已经带着值的栏位涂黑再截图，免得把预填的个人信息发出去。 */
  mask_sensitive: boolean;
}

export interface BlackRow {
  brand_id: string;
  name: string;
  reason: string;
  source: string;
  source_label: string;
  created_at: string;
}

/** 网申填表：文件代传与学习开关，最后一步提交永远留给用户。 */
export interface Auto {
  /** 交付口径：宽松 loose / 中档 balanced / 严格 strict。 */
  handoff: string;
  /** 文件栏（简历、寸照）是否也自动上传。 */
  upload_files: boolean;
  /** 你在官网页面里手填的、或对照表上手改的那几条，回读后是否记进本机资料库。 */
  learn_web: boolean;
  /** 模型推算出来的栏，点「填入浏览器」认可后是否存进本机资料库。 */
  learn_ai: boolean;
}

export interface ReplyCfg {
  enabled: boolean;
  prompt: string;
  confirm_before_send: boolean;
  follow_up_hours: string;
  block_proactive_hr: boolean;
  pace: Pace;
}

/** 网申资料：企业官网投递表单要填的结构化字段（键为字段名，值只存本机）。 */
export interface ApplyProfileGroup {
  /** 分组标题，如「基本信息」「教育背景」。 */
  title: string;
  /** 这一组包含的字段键，顺序即界面顺序。 */
  keys: string[];
}

export interface ApplyProfile {
  labels: Record<string, string>;
  values: Record<string, string>;
  /** 字段分组，界面按组渲染；由后端给出，前端不再各存一份。 */
  groups: ApplyProfileGroup[];
  /** 本机学会的字段：在别家官网手工补过、资料库里本来没有的那几条。 */
  learned: ApplyLearnedRow[];
  /** 信息树：分组 → 字段 → 别名，供「树形」视图用。 */
  tree: ApplyTree;
  ready: boolean;
}

/** 一条「本机学会的字段」：某个官网手工填过、被记进资料库的那条。 */
export interface ApplyLearnedRow {
  key: string;
  label: string;
  value: string;
  /** 第一次学到的站点。 */
  domain: string;
  /** 来源：手动 / AI推算。 */
  source: string;
  /** 推算依据；手填的为空。 */
  basis: string;
  /** 推算可信度；0 表示没打分。 */
  confidence: number;
  updated_at: string;
}

/** 树上一条别名（共指项）：同一个字段认下过的另一种叫法。 */
export interface ApplyAliasNode {
  text: string;
  /** 是不是内置别名表里就有的；不是的话就是用户手动合并上去的。 */
  builtin: boolean;
}

/** 信息树上的一片叶子：一条字段资料。 */
export interface ApplyTreeNode {
  key: string;
  label: string;
  /** 内容。 */
  value: string;
  /** 本机填没填。 */
  filled: boolean;
  /** 来源：资料 / 简历 / 推算 / 记忆 / 自定义 / 未填 / 手动 / AI推算。 */
  source: string;
  /** 推算依据。 */
  note: string;
  /** 认下过的别名（共指项）。 */
  aliases: ApplyAliasNode[];
  /** 归并到的规范键。 */
  canonical: string;
  /** 待归并：叫法内置表认不出、用户也没合并过。 */
  orphan: boolean;
  kind: string;
  /** 这条的值是本机在别的官网学到 / 模型推出来的，不是「网申资料」里写的那份。 */
  memory?: boolean;
  /** 值是模型推的、用户还没核对过（树上标「待核对」，可「忘掉这条」）。 */
  pending?: boolean;
  /** 值来自本机记忆时，对应 ``apply_learned`` 里的那个键；删掉它用这个。 */
  forget_key?: string;
  /** 学到的字段才有：来源站点与时间。 */
  domain?: string;
  updated_at?: string;
}

export interface ApplyTreeGroup {
  title: string;
  /** 分组类别：group / learned / inferred。 */
  kind: string;
  fields: ApplyTreeNode[];
}

export interface ApplyTree {
  groups: ApplyTreeGroup[];
  /** 手动合并关系：{被并掉的叫法: 规范键}。 */
  merges: Record<string, string>;
  /** 内置别名表认不出的那些键。 */
  orphans: string[];
}

export interface Bundle {
  query: string;
  keywords: string[];
  cities: string[];
  experience: string[];
  education: string[];
  funding: string[];
  scale: string[];
  job_type_label: string;
  salary_label: string;
  pace: Pace;
  scam_block: boolean;
  resume_match: boolean;
  score_filter: boolean;
  min_score: number;
  salary_min: string;
  salary_max: string;
  resume_path: string;
  resume_size: number;
  resume_text: string;
  photo_path: string;
  photo_size: number;
  tailor: Tailor;
  reply: ReplyCfg;
  llm: Llm;
  captcha: Captcha;
  locate: Locate;
  vision: Vision;
  auto: Auto;
  blacklist: BlackRow[];
  options: Options;
  apply_profile: ApplyProfile;
}

export type ListField = "keywords" | "cities" | "experience" | "education" | "funding" | "scale";

export interface SearchForm {
  query: string;
  keywords: string[];
  cities: string[];
  experience: string[];
  education: string[];
  funding: string[];
  scale: string[];
  job_type_label: string;
  salary_label: string;
  pace_label: string;
  pace_params: Record<string, number>;
  scam_block: boolean;
  resume_match: boolean;
  score_filter: boolean;
  min_score: number;
  salary_min: string;
  salary_max: string;
  resume_path: string;
  resume_size: number;
  resume_text: string;
  photo_path: string;
  photo_size: number;
}

export interface ReplyForm {
  enabled: boolean;
  prompt: string;
  confirm_before_send: boolean;
  follow_up_hours: string;
  block_proactive_hr: boolean;
  pace_label: string;
  pace_params: Record<string, number>;
}

export interface LlmForm {
  base_url: string;
  api_key: string;
  key_set: boolean;
  model: string;
}
