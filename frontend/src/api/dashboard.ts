import { get } from './http'
import type { DashboardStats, ErrorDistributionItem, TrendPoint } from '@/types'

/** 仪表盘统计：GET /api/dashboard/stats */
export function getDashboardStats(): Promise<DashboardStats> {
  return get<DashboardStats>('/dashboard/stats')
}

/** 近7天审核趋势：GET /api/dashboard/trend */
export function getDashboardTrend(): Promise<TrendPoint[]> {
  return get<TrendPoint[]>('/dashboard/trend')
}

/** 问题类型分布：GET /api/dashboard/error-distribution */
export function getErrorDistribution(): Promise<ErrorDistributionItem[]> {
  return get<ErrorDistributionItem[]>('/dashboard/error-distribution')
}
