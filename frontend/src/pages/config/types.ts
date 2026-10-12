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

export interface BlackRow {
  brand_id: string;
  name: string;
  reason: string;
  source: string;
  source_label: string;
  created_at: string;
}

export interface ReplyCfg {
  enabled: boolean;
  prompt: string;
  confirm_before_send: boolean;
  follow_up_hours: string;
  block_proactive_hr: boolean;
  pace: Pace;
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
  resume_text: string;
  tailor: Tailor;
  reply: ReplyCfg;
  llm: Llm;
  blacklist: BlackRow[];
  options: Options;
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
  resume_text: string;
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
