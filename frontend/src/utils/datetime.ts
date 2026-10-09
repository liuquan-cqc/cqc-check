import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'

dayjs.extend(utc)
dayjs.extend(timezone)

const HAS_TIMEZONE = /(Z|[+-]\d{2}:?\d{2})$/i

/**
 * 后端历史数据为无偏移 UTC，新接口为带 Z 的 UTC。
 * 两种格式都按系统设置的时区显示，避免被浏览器误当成本地时间。
 */
export function formatSystemTime(
  value?: string | null,
  targetTimezone = 'Asia/Shanghai',
  format = 'YYYY-MM-DD HH:mm'
): string {
  if (!value) return '-'
  const parsed = HAS_TIMEZONE.test(value) ? dayjs(value) : dayjs.utc(value)
  if (!parsed.isValid()) return '-'
  try {
    return parsed.tz(targetTimezone || 'Asia/Shanghai').format(format)
  } catch {
    return parsed.tz('Asia/Shanghai').format(format)
  }
}
