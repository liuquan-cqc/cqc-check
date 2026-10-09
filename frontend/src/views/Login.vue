<template>
  <div class="login-page">
    <!-- 背景装饰：深蓝渐变 + 几何线条 -->
    <svg class="bg-lines" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <defs>
        <linearGradient id="lineGrad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="0.18" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      <path d="M-100 700 Q 400 500 800 620 T 1600 480" fill="none" stroke="url(#lineGrad)" stroke-width="2" />
      <path d="M-100 780 Q 420 580 820 700 T 1620 560" fill="none" stroke="url(#lineGrad)" stroke-width="1.5" />
      <path d="M-100 860 Q 440 660 840 780 T 1640 640" fill="none" stroke="url(#lineGrad)" stroke-width="1" />
      <circle cx="1150" cy="180" r="90" fill="none" stroke="url(#lineGrad)" stroke-width="1.5" />
      <circle cx="1240" cy="260" r="140" fill="none" stroke="url(#lineGrad)" stroke-width="1" />
    </svg>

    <div class="login-body">
      <!-- 左侧品牌区（窄屏隐藏） -->
      <div class="brand-area">
        <div class="brand-badge">CCC</div>
        <h1 class="brand-title">电线电缆检测报告审核系统</h1>
        <p class="brand-slogan">专业 · 高效 · 准确 · 安全</p>
        <p class="brand-desc">
          基于智能识别与标准核对的检测报告审核平台<br />
          助力认证审核数字化、智能化
        </p>
      </div>

      <!-- 登录卡片 -->
      <div class="login-card">
        <div class="card-badge">
          <el-icon :size="30"><CircleCheckFilled /></el-icon>
        </div>
        <h2 class="card-title">欢迎登录</h2>
        <p class="card-sub">请使用您的账号登录系统</p>

        <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="onSubmit">
          <el-form-item prop="username">
            <el-input v-model="form.username" placeholder="请输入用户名" :prefix-icon="User" />
          </el-form-item>
          <el-form-item prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入密码"
              show-password
              :prefix-icon="Lock"
            />
          </el-form-item>

          <!-- 表单内错误提示 -->
          <div v-if="errorMsg" class="error-tip">
            <el-icon><WarningFilled /></el-icon>
            <span>{{ errorMsg }}</span>
          </div>

          <el-form-item>
            <el-checkbox v-model="form.remember">记住我</el-checkbox>
          </el-form-item>

          <el-button class="submit-btn" type="primary" size="large" :loading="loading" @click="onSubmit">
            登 录
          </el-button>
        </el-form>

        <p class="card-footer">© 2024 中国质量认证中心 保留所有权利</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, CircleCheckFilled, WarningFilled } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const errorMsg = ref('')

const form = reactive({
  username: '',
  password: '',
  // 记住我：当前版本统一存 localStorage（简化实现）；
  // 如需"不记住仅本次会话"，可改为 sessionStorage 并在 http.ts/auth store 同步调整
  remember: true
})

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }]
}

/** 提交登录 */
async function onSubmit() {
  errorMsg.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.login(form.username, form.password)
    // 登录成功后拉取最新用户信息
    await auth.fetchMe().catch(() => undefined)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/dashboard'
    router.push(redirect)
  } catch (err: unknown) {
    // 401 等错误已在拦截器弹 toast，这里补充表单内红色提示
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    errorMsg.value = detail || '登录失败，请检查用户名和密码'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100%;
  background: linear-gradient(135deg, #16294a 0%, #1e3a5f 45%, #2563eb 130%);
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
}

.bg-lines {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.login-body {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 80px;
  padding: 0 40px;
  max-width: 1100px;
  width: 100%;
  justify-content: space-between;
}

/* ===== 左侧品牌区 ===== */
.brand-area {
  color: #fff;
  flex: 1;
  min-width: 0;
}

.brand-badge {
  width: 56px;
  height: 56px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.12);
  border: 1px solid rgba(255, 255, 255, 0.25);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 18px;
  margin-bottom: 24px;
}

.brand-title {
  font-size: 34px;
  font-weight: 700;
  margin: 0 0 14px;
  letter-spacing: 2px;
}

.brand-slogan {
  font-size: 15px;
  letter-spacing: 6px;
  opacity: 0.85;
  margin: 0 0 28px;
}

.brand-desc {
  font-size: 14px;
  line-height: 1.9;
  opacity: 0.65;
  margin: 0;
}

/* ===== 登录卡片 ===== */
.login-card {
  width: 400px;
  flex-shrink: 0;
  background: var(--el-bg-color);
  border-radius: 14px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25);
  padding: 36px 36px 20px;
  text-align: center;
}

.card-badge {
  width: 56px;
  height: 56px;
  margin: 0 auto 12px;
  border-radius: 14px;
  background: var(--ccc-primary);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
}

.card-title {
  margin: 0;
  font-size: 22px;
  font-weight: 700;
}

.card-sub {
  margin: 6px 0 24px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.login-card :deep(.el-form-item) {
  margin-bottom: 18px;
}

.login-card :deep(.el-checkbox) {
  height: auto;
}

.error-tip {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--el-color-danger);
  font-size: 13px;
  background: var(--el-color-danger-light-9);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 14px;
}

.submit-btn {
  width: 100%;
  letter-spacing: 8px;
  font-weight: 600;
}

.card-footer {
  margin: 28px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* 平板适配：隐藏品牌区，卡片居中 */
@media (max-width: 900px) {
  .brand-area {
    display: none;
  }

  .login-body {
    justify-content: center;
  }

  .login-card {
    width: min(400px, 92vw);
  }
}
</style>
