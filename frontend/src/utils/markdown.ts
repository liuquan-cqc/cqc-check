/**
 * 将审核数值范围中的单个半角波浪号转成全角范围符号。
 * marked 会把多个单 ~ 两两配对为 <del>，但真正的 ~~删除线~~ 不应受影响。
 */
export function normalizeNumericRanges(markdown: string): string {
  return String(markdown || '').replace(/([0-9%℃°])~(?=[+\-−]?\d)/g, '$1～')
}
