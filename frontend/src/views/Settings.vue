<template>
  <div class="settings-page">
    <el-skeleton v-if="loading" :rows="10" animated class="settings-skeleton" />

    <el-alert v-else-if="loadError" type="error" :closable="false" show-icon>
      <template #title>系统设置加载失败</template>
      <el-button link type="primary" @click="loadAll">重新加载</el-button>
    </el-alert>

    <div v-else class="settings-shell">
      <aside class="settings-nav" aria-label="系统设置分类">
        <button
          v-for="item in navItems"
          :key="item.key"
          type="button"
          class="settings-nav-item"
          :class="{ active: activeSection === item.key }"
          @click="selectSection(item.key)"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </button>
      </aside>

      <main class="settings-main">
        <header class="settings-header">
          <div>
            <h2>{{ currentNav.label }}</h2>
            <p>{{ currentNav.description }}</p>
          </div>
          <el-button v-if="savableSections.includes(activeSection)" type="primary" :loading="saving" @click="saveCurrent">保存设置</el-button>
        </header>

        <section v-if="activeSection === 'basic'" class="settings-content">
          <div class="setting-group">
            <h3>基本信息</h3>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="系统名称"><el-input v-model="settings.basic.system_name" maxlength="60" /></el-form-item>
              <el-form-item label="系统 Logo">
                <div class="logo-field">
                  <div class="logo-preview"><img v-if="settings.basic.logo_data_url" :src="settings.basic.logo_data_url" alt="系统 Logo" /><span v-else>CQC</span></div>
                  <div>
                    <el-upload :auto-upload="false" :show-file-list="false" accept="image/png,image/jpeg" :on-change="onLogoChange"><el-button>更换 Logo</el-button></el-upload>
                    <div class="field-help">支持 PNG、JPG，建议正方形，文件不超过 500 KB</div>
                  </div>
                </div>
              </el-form-item>
              <el-form-item label="系统版本"><el-input v-model="settings.basic.system_version" maxlength="30" /></el-form-item>
              <el-form-item label="版权信息"><el-input v-model="settings.basic.copyright" maxlength="80" /></el-form-item>
            </el-form>
          </div>

          <div class="setting-group">
            <h3>区域与显示</h3>
            <el-form label-position="top" class="form-grid three-columns">
              <el-form-item label="时区"><el-select v-model="settings.basic.timezone"><el-option label="中国标准时间 (UTC+08:00)" value="Asia/Shanghai" /><el-option label="协调世界时 (UTC)" value="UTC" /></el-select></el-form-item>
              <el-form-item label="日期格式"><el-select v-model="settings.basic.date_format"><el-option label="YYYY-MM-DD" value="YYYY-MM-DD" /><el-option label="YYYY/MM/DD" value="YYYY/MM/DD" /></el-select></el-form-item>
              <el-form-item label="时间格式"><el-select v-model="settings.basic.time_format"><el-option label="24 小时制" value="24h" /><el-option label="12 小时制" value="12h" /></el-select></el-form-item>
              <el-form-item label="系统语言"><el-select v-model="settings.basic.language"><el-option label="简体中文" value="zh-CN" /></el-select></el-form-item>
              <el-form-item label="每页显示数"><el-select v-model="settings.basic.page_size"><el-option v-for="size in [10, 20, 50, 100]" :key="size" :label="`${size} 条`" :value="size" /></el-select></el-form-item>
            </el-form>
          </div>

          <div class="setting-group">
            <h3>个性化</h3>
            <div class="preference-grid">
              <div class="preference-block"><label>主题模式</label><el-radio-group v-model="settings.basic.theme_mode"><el-radio-button value="system">跟随系统</el-radio-button><el-radio-button value="light">浅色</el-radio-button><el-radio-button value="dark">深色</el-radio-button></el-radio-group></div>
              <div class="preference-block"><label>主题色</label><el-color-picker v-model="settings.basic.primary_color" :predefine="brandColors" /></div>
              <div class="preference-block"><label>表格密度</label><el-radio-group v-model="settings.basic.table_density"><el-radio-button value="compact">紧凑</el-radio-button><el-radio-button value="comfortable">舒适</el-radio-button><el-radio-button value="spacious">宽松</el-radio-button></el-radio-group></div>
            </div>
          </div>
        </section>

        <section v-else-if="activeSection === 'users'" class="settings-content">
          <div class="section-actions"><el-input v-model="userQuery" clearable placeholder="搜索用户名" :prefix-icon="Search" /><el-button type="primary" :icon="Plus" @click="openCreateDialog">新增用户</el-button></div>
          <el-table :data="filteredUsers" stripe>
            <el-table-column prop="username" label="用户名" min-width="150" />
            <el-table-column label="角色" width="120"><template #default="{ row }"><el-tag :type="roleTag(row.role)">{{ roleLabels[row.role] }}</el-tag></template></el-table-column>
            <el-table-column label="权限方式" min-width="150"><template #default="{ row }"><el-tag v-if="row.role === 'admin'" type="danger" effect="plain">管理员全部权限</el-tag><el-tag v-else-if="row.permissions_override !== null && row.permissions_override !== undefined" type="warning" effect="plain">专属权限 {{ row.permissions_override.length }} 项</el-tag><el-tag v-else type="info" effect="plain">继承角色</el-tag></template></el-table-column>
            <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '启用' : '禁用' }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间" width="175"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" min-width="260" align="right"><template #default="{ row }"><el-button link type="primary" @click="openEditDialog(row)">编辑</el-button><el-button link type="primary" @click="openResetDialog(row)">重置密码</el-button><el-button link :type="row.status === 'active' ? 'danger' : 'success'" :disabled="row.id === auth.user?.id" @click="toggleUser(row)">{{ row.status === 'active' ? '禁用' : '启用' }}</el-button></template></el-table-column>
          </el-table>
        </section>

        <section v-else-if="activeSection === 'roles'" class="settings-content">
          <el-alert type="info" :closable="false" show-icon title="管理员始终保留全部权限，避免误配置后无法进入系统设置。" />
          <div class="permission-matrix">
            <div class="permission-head permission-name">权限项</div><div v-for="role in roleKeys" :key="role" class="permission-head">{{ roleLabels[role] }}</div>
            <template v-for="permission in permissionItems" :key="permission.key">
              <div class="permission-name"><strong>{{ permission.label }}</strong><span>{{ permission.description }}</span></div>
              <div v-for="role in roleKeys" :key="`${permission.key}-${role}`" class="permission-cell"><el-checkbox :model-value="settings.roles[role]?.includes(permission.key)" :disabled="role === 'admin' || Boolean(permission.adminOnly)" @change="(checked: boolean) => setRolePermission(role, permission.key, checked)" /></div>
            </template>
          </div>
        </section>

        <section v-else-if="activeSection === 'notifications'" class="settings-content">
          <div class="setting-group"><h3>通知事件</h3><div class="switch-list"><div><span><strong>浏览器通知</strong><small>审核状态变化时在页面内提醒</small></span><el-switch v-model="settings.notifications.browser_enabled" /></div><div><span><strong>审核完成</strong><small>任务正常结束后发送通知</small></span><el-switch v-model="settings.notifications.notify_on_done" /></div><div><span><strong>审核失败</strong><small>模型或解析发生错误时发送通知</small></span><el-switch v-model="settings.notifications.notify_on_failed" /></div></div></div>
          <div class="setting-group">
            <div class="group-title-row"><h3>邮件通知</h3><el-switch v-model="settings.notifications.email_enabled" /></div>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="SMTP 服务器"><el-input v-model="settings.notifications.smtp_host" :disabled="!settings.notifications.email_enabled" placeholder="smtp.example.com" /></el-form-item>
              <el-form-item label="端口"><el-input-number v-model="settings.notifications.smtp_port" :disabled="!settings.notifications.email_enabled" :min="1" :max="65535" controls-position="right" /></el-form-item>
              <el-form-item label="账号"><el-input v-model="settings.notifications.smtp_username" :disabled="!settings.notifications.email_enabled" /></el-form-item>
              <el-form-item label="密码或授权码"><el-input v-model="settings.notifications.smtp_password" :disabled="!settings.notifications.email_enabled" type="password" show-password /></el-form-item>
              <el-form-item label="发件地址"><el-input v-model="settings.notifications.sender_email" :disabled="!settings.notifications.email_enabled" /></el-form-item>
              <el-form-item label="通知收件地址"><el-input v-model="settings.notifications.recipient_email" :disabled="!settings.notifications.email_enabled" /></el-form-item>
              <el-form-item label="SSL 加密"><el-switch v-model="settings.notifications.smtp_ssl" :disabled="!settings.notifications.email_enabled" /></el-form-item>
            </el-form>
          </div>
          <div class="setting-group"><div class="group-title-row"><h3>Webhook</h3><el-switch v-model="settings.notifications.webhook_enabled" /></div><el-form label-position="top"><el-form-item label="Webhook 地址"><el-input v-model="settings.notifications.webhook_url" :disabled="!settings.notifications.webhook_enabled" type="password" show-password placeholder="https://..." /></el-form-item></el-form></div>
        </section>

        <section v-else-if="activeSection === 'ocr'" class="settings-content">
          <div class="audit-flow" aria-label="当前审核流程">
            <div><b>1</b><span><strong>一次解析</strong><small>整份 PDF 提取文字、页码和坐标</small></span></div>
            <div><b>2</b><span><strong>按需修复</strong><small>仅疑难表格调用内网视觉模型</small></span></div>
            <div><b>3</b><span><strong>样品审核</strong><small>主模型审核后由程序复核汇总</small></span></div>
          </div>
          <div class="setting-group">
            <div class="group-title-row"><div><h3>报告一次解析</h3><p>优先使用 PDF 文字层，整份提取页码、坐标和表格；只有证据不足页才局部补充识别</p></div><el-switch v-model="settings.ocr.enabled" :disabled="settings.ocr.engine === 'paddleocr'" /></div>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="文档识别引擎"><el-select v-model="settings.ocr.engine" :disabled="!settings.ocr.enabled"><el-option label="PaddleOCR（公网 · 当前审核流程）" value="paddleocr" /><el-option label="RapidOCR（本地）" value="rapidocr" /><el-option label="MinerU 文档解析" value="mineru" /><el-option label="MinerU 混合模式" value="mineru_hybrid" /><el-option v-if="settings.ocr.engine === 'hybrid'" label="本地混合模式（历史配置）" value="hybrid" /></el-select></el-form-item>
              <el-form-item v-if="settings.ocr.engine !== 'paddleocr'" label="识别语言"><el-select v-model="settings.ocr.language" :disabled="!settings.ocr.enabled"><el-option label="简体中文 + 英文" value="zh-CN" /></el-select></el-form-item>
              <el-form-item v-if="settings.ocr.engine !== 'paddleocr'" label="PDF 渲染精度"><el-slider v-model="settings.ocr.render_dpi" :min="100" :max="400" :step="50" show-input /></el-form-item>
              <el-form-item v-if="settings.ocr.engine !== 'paddleocr'" label="视觉识别并发页数"><el-input-number v-model="settings.ocr.vision_concurrency" :min="1" :max="8" controls-position="right" /><div class="field-help">建议保持 3；数值过高可能触发 API 限流。</div></el-form-item>
              <el-form-item v-if="settings.ocr.engine !== 'paddleocr'" label="复杂表格使用视觉模型"><el-switch v-model="settings.ocr.dense_table_vision" /></el-form-item>
            </el-form>
            <template v-if="settings.ocr.engine === 'paddleocr'">
              <el-divider content-position="left">PaddleOCR 解析服务</el-divider>
              <div class="group-title-row"><div><h3>连接配置</h3><p>留空保留现有凭证；模型与识别选项沿用已验证配置。</p></div><el-tag :type="settings.ocr.paddleocr_status?.configured ? 'success' : 'warning'">{{ settings.ocr.paddleocr_status?.configured ? '凭证已配置' : '凭证未配置或不可读' }}</el-tag></div>
              <el-form label-position="top" class="form-grid two-columns">
                <el-form-item label="API 地址（固定）" class="full-row"><el-input :model-value="settings.ocr.paddleocr_status?.endpoint" readonly /></el-form-item>
                <el-form-item label="模型版本（固定）"><el-input :model-value="settings.ocr.paddleocr_status?.model" readonly /></el-form-item>
                <el-form-item label="当前凭证来源"><el-input :model-value="paddleCredentialSource" readonly /></el-form-item>
                <el-form-item label="PaddleOCR API Token" class="full-row"><el-input v-model="settings.ocr.paddleocr_api_key" type="password" show-password autocomplete="new-password" placeholder="留空沿用已配置的凭证；填写新密钥后保存" /><div class="field-help">新密钥加密保存后优先使用。留空或保留掩码不会清除已存密钥；服务器托管密钥不会回显。连接测试使用已保存配置。</div></el-form-item>
                <el-form-item label="当前识别方式" class="full-row"><span>整份 PDF 一次上传解析，保留原始页码和表格结构；仅在原页证据不足时对必要页面做局部补充识别。</span></el-form-item>
                <el-form-item label="识别选项（当前固定配置）" class="full-row"><span>文档方向分类：{{ settings.ocr.paddleocr_status?.options?.useDocOrientationClassify ? '开启' : '关闭' }}；文档矫正：{{ settings.ocr.paddleocr_status?.options?.useDocUnwarping ? '开启' : '关闭' }}；图表识别：{{ settings.ocr.paddleocr_status?.options?.useChartRecognition ? '开启' : '关闭' }}。</span></el-form-item>
              </el-form>
              <div class="inline-actions"><el-button :loading="testingPaddle" :disabled="!settings.ocr.paddleocr_status?.configured" @click="testPaddle">测试PaddleOCR连接</el-button><span>发送不含业务资料的测试图片，可能消耗OCR额度；不会上传报告。</span></div>
              <el-alert class="full-row" type="warning" :closable="false" show-icon title="公网OCR会接收原始报告页面；内网脱敏和外发安全网保护的是后续发送给外网审核模型的内容。Paddle主流程需保持OCR开启。" />
            </template>
            <template v-if="['mineru', 'mineru_hybrid'].includes(settings.ocr.engine)">
            <el-divider content-position="left">MinerU 备用解析</el-divider>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="API 地址" class="full-row"><el-input v-model="settings.ocr.mineru_base_url" placeholder="https://mineru.net/api/v4" /></el-form-item>
              <el-form-item label="任务接口"><el-input v-model="settings.ocr.mineru_task_endpoint" disabled /></el-form-item>
              <el-form-item label="模型版本"><el-select v-model="settings.ocr.mineru_model_version"><el-option label="VLM（版面/表格优先）" value="vlm" /><el-option label="Pipeline（标准解析）" value="pipeline" /></el-select></el-form-item>
              <el-form-item label="API Token" class="full-row"><el-input v-model="settings.ocr.mineru_api_key" type="password" show-password autocomplete="new-password" placeholder="Bearer Token" /><div class="field-help">Token 仅保存在本系统服务器，不会显示明文。</div></el-form-item>
              <el-form-item label="单份报告最长等待（秒）"><el-input-number v-model="settings.ocr.mineru_timeout_seconds" :min="60" :max="1800" controls-position="right" /></el-form-item>
              <el-form-item label="任务轮询间隔（秒）"><el-input-number v-model="settings.ocr.mineru_poll_interval_seconds" :min="2" :max="30" controls-position="right" /></el-form-item>
              <el-alert class="full-row" type="info" :closable="false" show-icon title="填写单位内网 MinerU 地址后，原始 PDF 与 OCR 结果无需离开单位网络。" />
            </el-form>
            </template>
            <el-alert v-if="settings.ocr.engine !== 'paddleocr'" type="warning" :closable="false" show-icon title="关闭 OCR 后，扫描件将交给视觉模型处理；请确保 AI 审核设置中的视觉模型可用。" />
          </div>
          <div class="setting-group">
            <div class="group-title-row">
              <div><h3>确定性表格行列修复</h3><p>优先使用文字层、原 PDF 坐标和 OCR 单元格证据，不调用内网模型</p></div>
              <el-tag type="info">内网模型已停用</el-tag>
            </div>
            <el-alert type="info" :closable="false" show-icon title="坐标和文字层无法唯一确定的行列关系直接转人工复核，不由模型猜测或补写数值。" />
            <div class="repair-status-row">
              <span>证据来源</span><strong>文字层 + 坐标 + OCR</strong>
              <span>模型补全</span><strong>已关闭</strong>
              <span>冲突处理</span><strong>保留原证据并转人工</strong>
            </div>
          </div>
        </section>

        <section v-else-if="activeSection === 'ai'" class="settings-content">
          <div class="setting-group">
            <div class="group-title-row"><div><h3>模型服务</h3><p>兼容 OpenAI Chat Completions 协议</p></div><el-tag :type="settings.ai.mock_enabled ? 'warning' : 'success'">{{ settings.ai.mock_enabled ? '演示模式' : '真实调用' }}</el-tag></div>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="当前启用服务商"><el-select v-model="settings.ai.provider"><el-option label="硅基流动" value="siliconflow" /><el-option label="DeepSeek 官方" value="deepseek" /><el-option label="单位内网模型（已停用）" value="intranet" disabled /><el-option label="其他 OpenAI 兼容服务" value="openai-compatible" /></el-select></el-form-item>
              <el-form-item label="演示模式"><el-switch v-model="settings.ai.mock_enabled" active-text="返回固定测试结果" /></el-form-item>
              <template v-if="settings.ai.provider === 'deepseek'">
                <el-form-item label="DeepSeek API 地址" class="full-row"><el-input v-model="settings.ai.deepseek_base_url" placeholder="https://api.deepseek.com" /></el-form-item>
                <el-form-item label="DeepSeek API Key" class="full-row"><el-input v-model="settings.ai.deepseek_api_key" type="password" show-password autocomplete="new-password" placeholder="sk-..." /><div class="field-help">与硅基流动密钥分开加密保存；切换服务商不会覆盖另一套配置。</div></el-form-item>
                <el-form-item label="审核模型"><el-input v-model="settings.ai.deepseek_review_model" /></el-form-item><el-form-item label="视觉 OCR 模型"><el-input v-model="settings.ai.deepseek_vision_model" /></el-form-item>
                <el-form-item label="DeepSeek 生成温度"><el-slider v-model="settings.ai.deepseek_temperature" :min="0" :max="1" :step="0.1" show-input /></el-form-item>
                <el-form-item label="DeepSeek 最大输出 Token"><el-input-number v-model="settings.ai.deepseek_max_tokens" :min="256" :max="32768" :step="256" controls-position="right" /><div class="field-help">V4 推理模型建议不低于 16384，避免推理耗尽额度后正文为空。</div></el-form-item>
                <el-form-item label="DeepSeek 请求超时（秒）"><el-input-number v-model="settings.ai.deepseek_timeout_seconds" :min="10" :max="600" controls-position="right" /></el-form-item>
                <el-form-item label="DeepSeek 思考模式"><el-switch v-model="settings.ai.deepseek_thinking_enabled" active-text="开启" inactive-text="关闭" /><div class="field-help">只影响 DeepSeek 官方源，不会改动硅基流动或单位内网模型。</div></el-form-item>
                <el-form-item v-if="settings.ai.deepseek_thinking_enabled" label="DeepSeek 思考强度"><el-select v-model="settings.ai.deepseek_reasoning_effort"><el-option label="low（低）" value="low" /><el-option label="high（高）" value="high" /><el-option label="max（最高）" value="max" /></el-select><div class="field-help">官方 Chat Completions 参数 reasoning_effort。medium/xhigh 均会映射为 high，因此不重复展示。</div></el-form-item>
              </template>
              <template v-else-if="settings.ai.provider === 'intranet'">
                <el-alert class="full-row" type="info" :closable="false" show-icon title="主审核也将使用下方“内网表格视觉预处理”区域中的连接和审核模型配置。" />
              </template>
              <template v-else>
                <el-form-item label="API 基础地址" class="full-row"><el-input v-model="settings.ai.base_url" placeholder="https://api.siliconflow.cn/v1" /></el-form-item>
                <el-form-item label="API Key" class="full-row"><el-input v-model="settings.ai.api_key" type="password" show-password autocomplete="new-password" placeholder="sk-..." /><div class="field-help">这是原有硅基流动/兼容服务配置；已保存的密钥不会回传明文。</div></el-form-item>
                <el-form-item label="审核模型"><el-input v-model="settings.ai.review_model" /></el-form-item><el-form-item label="视觉 OCR 模型"><el-input v-model="settings.ai.vision_model" /></el-form-item>
                <el-form-item label="生成温度"><el-slider v-model="settings.ai.temperature" :min="0" :max="1" :step="0.1" show-input /></el-form-item>
                <el-form-item label="最大输出 Token"><el-input-number v-model="settings.ai.max_tokens" :min="256" :max="32768" :step="256" controls-position="right" /></el-form-item>
                <el-form-item label="请求超时（秒）"><el-input-number v-model="settings.ai.timeout_seconds" :min="10" :max="600" controls-position="right" /></el-form-item>
              </template>
            </el-form>
            <el-alert class="provider-note" type="info" :closable="false" show-icon title="第一阶段内容复核和第二阶段问题审核使用同一个当前外网服务商与审核模型。" />
            <div class="inline-actions"><el-button :loading="testingAi" @click="testAi">测试当前服务商</el-button><span>请先保存，再测试当前启用的配置。</span></div>
          </div>
          <div class="setting-group">
            <div class="group-title-row">
              <div><h3>双阶段外网审核</h3><p>第一次查内部一致性，第二次结合结构化规则库查找报告问题</p></div>
              <el-tag :type="settings.ai.external_content_review_enabled ? 'success' : 'info'">{{ settings.ai.external_content_review_enabled ? '双阶段已开启' : '仅主审核' }}</el-tag>
            </div>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="启用第一阶段内容复核"><el-switch v-model="settings.ai.external_content_review_enabled" /><div class="field-help">只检查编号、日期、计算和 P/N/F 等报告内部矛盾。</div></el-form-item>
              <el-form-item label="当前双阶段模型"><el-input :model-value="settings.ai.provider === 'deepseek' ? settings.ai.deepseek_review_model : settings.ai.review_model" disabled /></el-form-item>
              <el-alert class="full-row" type="info" :closable="false" show-icon title="第一阶段不查标准限值或试验适用性，产生的线索必须由第二阶段回到原文和规则库核实，不直接生成‘必须修改’。" />
            </el-form>
          </div>
          <div class="setting-group">
            <div class="group-title-row"><div><h3>外发安全保护</h3><p>两次外网调用都必须先脱敏并经过最终残留检查</p></div><el-tag type="success">继续生效</el-tag></div>
            <div class="safety-status-list">
              <div><strong>企业名称脱敏</strong><span>{{ settings.ai.privacy_company_blacklist_enabled ? '已启用' : '未启用' }}</span></div>
              <div><strong>申请编号脱敏</strong><span>{{ settings.ai.privacy_application_blacklist_enabled ? '已启用' : '未启用' }}</span></div>
              <div><strong>外发前残留检查</strong><span>{{ settings.ai.privacy_outbound_block_enabled ? '已启用' : '未启用' }}</span></div>
            </div>
            <el-alert type="warning" :closable="false" show-icon title="外发前仍会执行确定性脱敏和残留检查；发现敏感字段时停止发送并转人工处理。" />
          </div>
        </section>

        <section v-else-if="activeSection === 'company_registry'" class="settings-content">
          <div class="setting-group">
            <div class="group-title-row">
              <div><h3>企业状态核验</h3><p>默认使用已登录 Edge 的天眼查免费网页次数，Cookie不离开浏览器</p></div>
              <el-tag :type="settings.company_registry.enabled ? 'success' : 'info'">
                {{ settings.company_registry.enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
            <el-form label-position="top" class="form-grid two-columns">
              <el-form-item label="启用企业核验"><el-switch v-model="settings.company_registry.enabled" /></el-form-item>
              <el-form-item label="核验方式"><el-select v-model="settings.company_registry.mode"><el-option label="Edge网页辅助（推荐）" value="browser" /><el-option label="天眼查付费API" value="api" /></el-select></el-form-item>
              <template v-if="settings.company_registry.mode === 'browser'">
                <el-form-item label="每日核验上限"><el-input-number v-model="settings.company_registry.web_daily_limit" :min="1" :max="50" controls-position="right" /></el-form-item>
                <el-form-item label="企业间查询间隔（秒）"><el-input-number v-model="settings.company_registry.web_interval_seconds" :min="10" :max="300" controls-position="right" /></el-form-item>
                <el-form-item label="安全边界" class="full-row"><el-alert type="success" :closable="false" show-icon title="扩展只读取当前天眼查页面可见的企业全称、登记状态和信用代码；不申请Cookie权限。" /></el-form-item>
              </template>
              <template v-else>
              <el-form-item label="接口地址" class="full-row">
                <el-input v-model="settings.company_registry.base_url" placeholder="https://api.tianyancha.com/v2/company/search" />
              </el-form-item>
              <el-form-item label="Token" class="full-row">
                <el-input v-model="settings.company_registry.token" type="password" show-password autocomplete="new-password" placeholder="请在此处粘贴天眼查 Token" />
                <div class="field-help">Token 在服务端加密保存，页面不会回传明文；保留星号表示不修改已有 Token。</div>
              </el-form-item>
              <el-form-item label="Token 鉴权方式">
                <el-select v-model="settings.company_registry.auth_mode"><el-option label="Bearer Token" value="bearer" /><el-option label="Authorization 直接传 Token" value="raw" /></el-select>
              </el-form-item>
              <el-form-item label="请求超时（秒）"><el-input-number v-model="settings.company_registry.timeout_seconds" :min="5" :max="120" controls-position="right" /></el-form-item>
              <el-form-item label="附加返回数据" class="full-row">
                <el-checkbox-group v-model="settings.company_registry.include_fields">
                  <el-checkbox value="shareholders">股东信息 shareholders</el-checkbox>
                  <el-checkbox value="risk">风险信息 risk</el-checkbox>
                </el-checkbox-group>
                <div class="field-help">存续状态核验不需要这两项；若天眼查套餐按字段计费，建议保持不勾选。</div>
              </el-form-item>
              </template>
            </el-form>
            <div v-if="settings.company_registry.mode === 'api'" class="inline-actions"><el-button :loading="testingCompanyRegistry" @click="testCompanyRegistry">测试API连接</el-button><span>请先保存设置，再测试当前配置。</span></div>
          </div>
          <el-alert type="info" :closable="false" show-icon title="只有您在 Edge 扩展中点击“开始核验”后，企业全称才会发送给天眼查；不发送PDF、审核结果或用户信息。" />
        </section>

        <section v-else-if="activeSection === 'files'" class="settings-content">
          <div class="setting-group"><h3>上传与存储</h3><el-form label-position="top" class="form-grid two-columns"><el-form-item label="单文件大小上限"><el-input-number v-model="settings.files.max_upload_mb" :min="1" :max="500" controls-position="right" /><span class="input-suffix">MB</span></el-form-item><el-form-item label="允许的文件格式"><el-select v-model="settings.files.allowed_extensions" multiple><el-option label="PDF" value="pdf" /></el-select></el-form-item><el-form-item label="文件保留天数"><el-input-number v-model="settings.files.retention_days" :min="0" :max="3650" controls-position="right" /><div class="field-help">0 表示永久保留。大于 0 时，Worker 会清理超过期限的报告及关联文件。</div></el-form-item></el-form></div>
          <el-alert type="info" :closable="false" show-icon title="上传文件与 OCR 缓存保存在服务器 data 目录；删除报告时会同步安全删除对应文件。" />
        </section>

        <section v-else-if="activeSection === 'workflow'" class="settings-content">
          <div class="setting-group"><h3>审核任务</h3><div class="switch-list"><div><span><strong>上传后自动审核</strong><small>创建报告后立即进入任务队列</small></span><el-switch v-model="settings.workflow.auto_review" /></div><div><span><strong>允许重新审核</strong><small>审核员可以重新发起模型审核</small></span><el-switch v-model="settings.workflow.allow_re_review" /></div></div><el-form label-position="top" class="form-grid three-columns workflow-fields"><el-form-item label="并发任务数"><el-input-number v-model="settings.workflow.worker_concurrency" :min="1" :max="16" controls-position="right" /></el-form-item><el-form-item label="队列轮询间隔（秒）"><el-input-number v-model="settings.workflow.poll_interval_seconds" :min="1" :max="60" controls-position="right" /></el-form-item><el-form-item label="失败重试次数"><el-input-number v-model="settings.workflow.max_retries" :min="0" :max="10" controls-position="right" /></el-form-item></el-form></div>
        </section>

        <section v-else-if="activeSection === 'security'" class="settings-content">
          <div class="setting-group public-access-group" :class="{ 'is-public': settings.security.public_access_enabled }">
            <div class="public-access-row">
              <div>
                <div class="public-access-title">
                  <h3>公网访问控制</h3>
                  <el-tag :type="settings.security.public_access_enabled ? 'success' : 'info'" effect="plain">
                    {{ settings.security.public_access_enabled ? '公网已开放' : '仅限内网' }}
                  </el-tag>
                </div>
                <p>控制报告审核网站的 Cloudflare 公网入口，关闭后不影响单位内网访问。</p>
              </div>
              <el-switch
                v-model="settings.security.public_access_enabled"
                :loading="publicAccessSaving"
                :disabled="publicAccessSaving"
                inline-prompt
                active-text="开"
                inactive-text="关"
                aria-label="公网访问开关"
                @change="onPublicAccessToggle"
              />
            </div>
            <div class="access-endpoints">
              <div><span>公网入口</span><strong>由反向代理配置</strong></div>
              <div><span>内网入口</span><strong>由部署环境配置</strong></div>
            </div>
            <el-alert
              :type="settings.security.public_access_enabled ? 'warning' : 'success'"
              :closable="false"
              show-icon
              :title="settings.security.public_access_enabled
                ? '公网用户可以看到登录页，但仍需使用系统账号登录。开关修改后立即生效。'
                : '公网请求已被系统拒绝，管理员可通过内网入口重新开启。'"
            />
          </div>
          <div class="setting-group"><h3>登录与密码</h3><el-form label-position="top" class="form-grid two-columns"><el-form-item label="登录有效期（小时）"><el-input-number v-model="settings.security.jwt_expire_hours" :min="1" :max="168" controls-position="right" /></el-form-item><el-form-item label="密码最小长度"><el-input-number v-model="settings.security.password_min_length" :min="6" :max="64" controls-position="right" /></el-form-item><el-form-item label="连续失败次数"><el-input-number v-model="settings.security.login_max_failures" :min="1" :max="20" controls-position="right" /></el-form-item><el-form-item label="锁定时间（分钟）"><el-input-number v-model="settings.security.lock_minutes" :min="1" :max="1440" controls-position="right" /></el-form-item></el-form><el-alert type="warning" :closable="false" show-icon title="登录有效期只影响新签发的登录凭证；已登录用户将在原凭证到期后重新登录。" /></div>
        </section>

        <section v-else-if="activeSection === 'browserSync'" class="settings-content">
          <div class="setting-group">
            <div class="group-title-row"><div><h3>Edge 浏览器辅助程序</h3><p>与Win11虚拟机中的Edge配对，接收单位系统下载的型式试验报告</p></div><div class="inline-actions"><el-tag type="success" effect="plain">只读下载</el-tag><el-button @click="downloadEdgeExtension">下载Edge扩展</el-button></div></div>
            <el-alert type="info" :closable="false" show-icon title="辅助程序不会读取USB密钥、证书私钥或单位系统Cookie；配对设备只能向本系统上传PDF。" />
            <el-form label-position="top" class="form-grid two-columns browser-sync-form">
              <el-form-item label="审核网站地址"><el-input :model-value="browserServerUrl" readonly /></el-form-item>
              <el-form-item label="一次性配对码">
                <div class="pairing-code-row"><strong>{{ pairingCode || '尚未生成' }}</strong><el-button type="primary" :loading="pairingLoading" @click="generatePairingCode">生成配对码</el-button></div>
                <div class="field-help">配对码10分钟内有效且只能使用一次。请在Win11 Edge扩展中填写。</div>
              </el-form-item>
            </el-form>
          </div>
          <div class="setting-group">
            <div class="group-title-row"><div><h3>已配对设备</h3><p>设备令牌仅保存哈希，可随时撤销</p></div><el-button @click="loadBrowserDevices">刷新</el-button></div>
            <el-table v-loading="browserDevicesLoading" :data="browserDevices" stripe>
              <el-table-column prop="name" label="设备名称" min-width="180" />
              <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '已启用' : '已撤销' }}</el-tag></template></el-table-column>
              <el-table-column label="最近同步" min-width="170"><template #default="{ row }">{{ row.last_seen_at ? formatTime(row.last_seen_at) : '尚未同步' }}</template></el-table-column>
              <el-table-column label="配对时间" min-width="170"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column>
              <el-table-column label="操作" width="100" align="right"><template #default="{ row }"><el-button v-if="row.status === 'active'" link type="danger" @click="revokeBrowserDevice(row)">撤销</el-button></template></el-table-column>
            </el-table>
          </div>
        </section>

        <section v-else-if="activeSection === 'logs'" class="settings-content">
          <div class="section-actions"><div class="log-filters"><el-select v-model="logFilters.module" clearable placeholder="全部模块" @change="loadLogs"><el-option v-for="module in ['系统设置', 'AI审核', '安全', '用户管理']" :key="module" :label="module" :value="module" /></el-select><el-input v-model="logFilters.q" clearable placeholder="搜索用户或操作" :prefix-icon="Search" @keyup.enter="loadLogs" /><el-button @click="loadLogs">查询</el-button></div></div>
          <el-table v-loading="logsLoading" :data="logs" stripe><el-table-column prop="created_at" label="时间" width="175"><template #default="{ row }">{{ formatTime(row.created_at) }}</template></el-table-column><el-table-column prop="username" label="用户" width="120" /><el-table-column prop="module" label="模块" width="110" /><el-table-column prop="action" label="操作" min-width="170" /><el-table-column prop="detail" label="详情" min-width="240" show-overflow-tooltip /><el-table-column prop="ip_address" label="IP 地址" width="145" /><el-table-column label="结果" width="80"><template #default="{ row }"><el-tag :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag></template></el-table-column></el-table>
          <el-pagination v-model:current-page="logFilters.page" :page-size="logFilters.size" :total="logTotal" layout="total, prev, pager, next" @current-change="loadLogs" />
        </section>
      </main>
    </div>

    <el-dialog v-model="userDialog.visible" :title="userDialog.mode === 'create' ? '新增用户' : '编辑用户权限'" width="min(720px, 94vw)" destroy-on-close>
      <el-form label-position="top">
        <div class="form-grid two-columns">
          <el-form-item label="用户名"><el-input v-model="userDialog.username" :disabled="userDialog.mode === 'edit'" maxlength="50" /></el-form-item>
          <el-form-item v-if="userDialog.mode === 'create'" label="初始密码"><el-input v-model="userDialog.password" type="password" show-password /></el-form-item>
          <el-form-item label="角色" :class="{ 'full-row': userDialog.mode === 'edit' }"><el-select v-model="userDialog.role" :disabled="userDialog.id === auth.user?.id" @change="onUserRoleChange"><el-option v-for="role in roleKeys" :key="role" :label="roleLabels[role]" :value="role" /></el-select></el-form-item>
        </div>
        <el-alert v-if="userDialog.role === 'admin'" type="info" :closable="false" show-icon title="管理员固定拥有全部权限，不能设置专属权限。" />
        <div v-else class="user-permission-editor">
          <div class="permission-mode-row">
            <div><strong>账号权限</strong><span>可继承角色模板，也可只开放选中的功能</span></div>
            <el-radio-group v-model="userDialog.permissionMode" @change="onPermissionModeChange">
              <el-radio-button value="role">继承角色</el-radio-button>
              <el-radio-button value="custom">自定义权限</el-radio-button>
            </el-radio-group>
          </div>
          <el-alert v-if="userDialog.permissionMode === 'role'" type="info" :closable="false" :title="`将使用“${roleLabels[userDialog.role]}”当前配置的 ${roleDefaultPermissions(userDialog.role).length} 项权限。角色模板以后调整时，该账号会同步变化。`" />
          <div v-else>
            <div class="custom-permission-summary"><span>已选择 <b>{{ userDialog.permissions.length }}</b> 项</span><small>报告操作会自动包含查看权限；用户管理和系统设置仅管理员可用</small></div>
            <el-checkbox-group v-model="userDialog.permissions" class="user-permission-options" @change="normalizeUserPermissions">
              <el-checkbox v-for="permission in delegablePermissionItems" :key="permission.key" :value="permission.key">
                <span><strong>{{ permission.label }}</strong><small>{{ permission.description }}</small></span>
              </el-checkbox>
            </el-checkbox-group>
          </div>
        </div>
      </el-form>
      <template #footer><el-button @click="userDialog.visible = false">取消</el-button><el-button type="primary" :loading="userSaving" @click="submitUser">保存用户</el-button></template>
    </el-dialog>
    <el-dialog v-model="resetDialog.visible" title="重置密码" width="420px" destroy-on-close><el-form label-position="top"><el-form-item :label="`为 ${resetDialog.username} 设置新密码`"><el-input v-model="resetDialog.password" type="password" show-password /></el-form-item></el-form><template #footer><el-button @click="resetDialog.visible = false">取消</el-button><el-button type="primary" :loading="userSaving" @click="submitReset">确认重置</el-button></template></el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, markRaw, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type UploadFile } from 'element-plus'
import { Bell, Connection, Cpu, DocumentChecked, Folder, Lock, MagicStick, Plus, Search, Setting, Tickets, User, UserFilled } from '@element-plus/icons-vue'
import { formatSystemTime } from '@/utils/datetime'
import { createUser, getUsers, updateUser } from '@/api/users'
import { getAuditLogs, getSystemSettings, saveSystemSettings, testAiConnection, testCompanyRegistryConnection, testPaddleConnection, type AuditLogItem, type SystemSettings } from '@/api/settings'
import { createBrowserPairingCode, downloadBrowserSyncExtension, getBrowserSyncDevices, revokeBrowserSyncDevice, type BrowserSyncDevice } from '@/api/browserSync'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'
import type { User as UserType } from '@/types'

const auth = useAuthStore()
const app = useAppStore()
const loading = ref(true)
const loadError = ref(false)
const saving = ref(false)
const publicAccessSaving = ref(false)
const testingAi = ref(false)
const testingCompanyRegistry = ref(false)
const testingPaddle = ref(false)
const activeSection = ref('basic')
const settings = reactive<SystemSettings>({ basic: {}, notifications: {}, ocr: {}, ai: {}, company_registry: {}, files: {}, workflow: {}, security: {}, roles: { admin: [], reviewer: [], viewer: [] } })
const paddleCredentialSource = computed(() => ({ database: '系统设置（加密保存）', server_file: '服务器托管凭证', environment: '服务器环境配置', none: '未配置' }[settings.ocr.paddleocr_status?.credential_source as string] || '待加载'))

async function testPaddle() {
  try { await ElMessageBox.confirm('使用已保存的Paddle配置，向公网服务发送一张仅含OCR CONNECTION TEST的测试图片。不会发送业务报告，可能消耗OCR额度。是否继续？', '测试PaddleOCR连接', { type: 'warning', confirmButtonText: '发送测试图片', cancelButtonText: '取消' }) } catch { return }
  testingPaddle.value = true
  try { const result = await testPaddleConnection(); ElMessage.success(result.message) } finally { testingPaddle.value = false }
}
const navItems = [
  { key: 'basic', label: '基本设置', description: '系统标识、区域格式与个性化显示', icon: markRaw(Setting) },
  { key: 'users', label: '用户管理', description: '创建账号、分配角色和重置密码', icon: markRaw(User) },
  { key: 'roles', label: '角色与权限', description: '配置审核员和查看员的功能权限', icon: markRaw(UserFilled) },
  { key: 'notifications', label: '通知设置', description: '配置浏览器、邮件和 Webhook 通知', icon: markRaw(Bell) },
  { key: 'ocr', label: 'OCR 设置', description: '控制扫描件识别与复杂表格处理', icon: markRaw(DocumentChecked) },
  { key: 'ai', label: 'AI 审核设置', description: '配置模型服务、密钥和生成参数', icon: markRaw(MagicStick) },
  { key: 'company_registry', label: '企业状态核验', description: '配置Edge网页辅助或天眼查API核验', icon: markRaw(Connection) },
  { key: 'files', label: '文件设置', description: '限制上传格式、大小与存储周期', icon: markRaw(Folder) },
  { key: 'workflow', label: '审核流程设置', description: '配置任务队列、并发和失败重试', icon: markRaw(Cpu) },
  { key: 'security', label: '安全设置', description: '控制登录有效期、密码和失败锁定', icon: markRaw(Lock) },
  { key: 'browserSync', label: '浏览器同步', description: '配对Win11 Edge并自动接收单位系统报告', icon: markRaw(Connection) },
  { key: 'logs', label: '操作日志', description: '查看管理员操作和登录安全记录', icon: markRaw(Tickets) }
]
const currentNav = computed(() => navItems.find((item) => item.key === activeSection.value) || navItems[0])
const savableSections = ['basic', 'roles', 'notifications', 'ocr', 'ai', 'company_registry', 'files', 'workflow', 'security']
const brandColors = ['#2563eb', '#0f766e', '#15803d', '#7c3aed', '#c2410c', '#b91c1c']
const roleKeys = ['admin', 'reviewer', 'viewer']
const roleLabels: Record<string, string> = { admin: '管理员', reviewer: '审核员', viewer: '查看员' }
const permissionItems = [
  { key: 'dashboard.view', label: '查看仪表盘', description: '访问统计卡片和趋势图' }, { key: 'reports.view', label: '查看报告', description: '查看报告列表、详情和 PDF' },
  { key: 'reports.upload', label: '上传报告', description: '上传新的检测报告' }, { key: 'reports.review', label: '发起审核', description: '重新提交 AI 审核任务' },
  { key: 'reports.download', label: '下载意见书', description: '查看和下载审核意见书' }, { key: 'reports.delete', label: '删除报告', description: '删除报告及关联文件' },
  { key: 'companies.manage', label: '企业管理', description: '新增、修改和删除企业' }, { key: 'users.manage', label: '用户管理', description: '维护账号、角色和状态', adminOnly: true },
  { key: 'settings.manage', label: '系统设置', description: '修改系统运行配置', adminOnly: true },
  { key: 'iteration.manage', label: '审核迭代', description: '确认纠错、管理规则和发布审核版本' },
  { key: 'sampling.manage', label: '下样管理', description: '管理员和审核员可生成、调整和追溯方案；仅管理员可删除历史' }
]
const delegablePermissionItems = permissionItems.filter((item) => !item.adminOnly)

async function loadAll() { loading.value = true; loadError.value = false; try { Object.assign(settings, await getSystemSettings()); await loadUsers() } catch { loadError.value = true } finally { loading.value = false } }
function selectSection(key: string) { activeSection.value = key; if (key === 'logs') loadLogs(); if (key === 'browserSync') loadBrowserDevices() }
async function saveCurrent() { saving.value = true; try { const key = activeSection.value as keyof SystemSettings; const result = await saveSystemSettings(activeSection.value, JSON.parse(JSON.stringify(settings[key]))); Object.assign(settings[key], result); if (activeSection.value === 'basic') await app.loadSystemConfig(); ElMessage.success('设置已保存') } finally { saving.value = false } }
async function onPublicAccessToggle(enabled: boolean) {
  const previous = !enabled
  const title = enabled ? '开启公网访问' : '关闭公网访问'
  const message = enabled
    ? '开启后，公网用户可以访问登录页，登录后按账号权限使用系统。确认开启吗？'
    : '关闭后，公网入口将立即停止访问，只能通过单位内网重新开启。确认关闭吗？'
  try {
    await ElMessageBox.confirm(message, title, {
      type: 'warning',
      confirmButtonText: enabled ? '确认开启' : '确认关闭',
      cancelButtonText: '取消'
    })
  } catch {
    settings.security.public_access_enabled = previous
    return
  }
  publicAccessSaving.value = true
  try {
    const result = await saveSystemSettings('security', JSON.parse(JSON.stringify(settings.security)))
    Object.assign(settings.security, result)
    ElMessage.success(enabled ? '公网访问已开启' : '公网访问已关闭，内网入口保持可用')
    if (!enabled && window.location.protocol === 'https:') {
      window.setTimeout(() => window.location.reload(), 900)
    }
  } catch {
    settings.security.public_access_enabled = previous
  } finally {
    publicAccessSaving.value = false
  }
}
function setRolePermission(role: string, permission: string, checked: boolean) { const list = settings.roles[role] || (settings.roles[role] = []); if (checked && !list.includes(permission)) list.push(permission); if (!checked) settings.roles[role] = list.filter((item) => item !== permission) }
function onLogoChange(uploadFile: UploadFile) { const file = uploadFile.raw; if (!file) return; if (file.size > 500 * 1024) return void ElMessage.error('Logo 文件不能超过 500 KB'); const reader = new FileReader(); reader.onload = () => { settings.basic.logo_data_url = String(reader.result || '') }; reader.readAsDataURL(file) }
async function testAi() { testingAi.value = true; try { const result = await testAiConnection(); ElMessage.success(`${result.message}：${result.model}`) } finally { testingAi.value = false } }
async function testCompanyRegistry() { testingCompanyRegistry.value = true; try { const result = await testCompanyRegistryConnection(); ElMessage.success(`${result.message}：${result.company}（${result.status}）`) } finally { testingCompanyRegistry.value = false } }

const browserServerUrl = window.location.origin
const pairingCode = ref('')
const pairingLoading = ref(false)
const browserDevicesLoading = ref(false)
const browserDevices = ref<BrowserSyncDevice[]>([])
async function generatePairingCode() { pairingLoading.value = true; try { const result = await createBrowserPairingCode(); pairingCode.value = result.code; ElMessage.success('一次性配对码已生成，有效期10分钟') } finally { pairingLoading.value = false } }
async function loadBrowserDevices() { browserDevicesLoading.value = true; try { browserDevices.value = await getBrowserSyncDevices() } finally { browserDevicesLoading.value = false } }
async function revokeBrowserDevice(device: BrowserSyncDevice) { await ElMessageBox.confirm(`撤销“${device.name}”后，该Edge将不能继续同步报告，确认撤销吗？`, '撤销设备配对', { type: 'warning' }); await revokeBrowserSyncDevice(device.id); await loadBrowserDevices(); ElMessage.success('设备配对已撤销') }
async function downloadEdgeExtension() { await downloadBrowserSyncExtension(); ElMessage.success('Edge扩展安装包已开始下载') }

const users = ref<UserType[]>([])
const userQuery = ref('')
const userSaving = ref(false)
const filteredUsers = computed(() => users.value.filter((item) => item.username.toLowerCase().includes(userQuery.value.toLowerCase())))
const userDialog = reactive({ visible: false, mode: 'create', id: 0, username: '', password: '', role: 'viewer', permissionMode: 'role', permissions: [] as string[] })
const resetDialog = reactive({ visible: false, id: 0, username: '', password: '' })
async function loadUsers() { users.value = await getUsers() }
function roleDefaultPermissions(role: string) { return [...(settings.roles[role] || [])] }
function openCreateDialog() { Object.assign(userDialog, { visible: true, mode: 'create', id: 0, username: '', password: '', role: 'viewer', permissionMode: 'role', permissions: [] }) }
function openEditDialog(user: UserType) { const custom = user.permissions_override !== null && user.permissions_override !== undefined; Object.assign(userDialog, { visible: true, mode: 'edit', id: user.id, username: user.username, password: '', role: user.role, permissionMode: custom ? 'custom' : 'role', permissions: custom ? [...(user.permissions_override || [])] : [] }) }
function onUserRoleChange() { if (userDialog.role === 'admin') { userDialog.permissionMode = 'role'; userDialog.permissions = []; return } if (userDialog.permissionMode === 'custom') userDialog.permissions = roleDefaultPermissions(userDialog.role).filter((permission) => delegablePermissionItems.some((item) => item.key === permission)) }
function onPermissionModeChange() { if (userDialog.permissionMode === 'custom' && !userDialog.permissions.length) userDialog.permissions = roleDefaultPermissions(userDialog.role).filter((permission) => delegablePermissionItems.some((item) => item.key === permission)) }
function normalizeUserPermissions() { const reportActions = ['reports.upload', 'reports.review', 'reports.download', 'reports.delete']; if (reportActions.some((permission) => userDialog.permissions.includes(permission)) && !userDialog.permissions.includes('reports.view')) userDialog.permissions.unshift('reports.view') }
function openResetDialog(user: UserType) { Object.assign(resetDialog, { visible: true, id: user.id, username: user.username, password: '' }) }
async function submitUser() { if (!userDialog.username.trim()) return void ElMessage.warning('请输入用户名'); if (userDialog.mode === 'create' && !userDialog.password) return void ElMessage.warning('请输入初始密码'); const permissionsOverride = userDialog.role === 'admin' || userDialog.permissionMode === 'role' ? null : [...userDialog.permissions]; userSaving.value = true; try { if (userDialog.mode === 'create') await createUser({ username: userDialog.username, password: userDialog.password, role: userDialog.role, permissions_override: permissionsOverride }); else await updateUser(userDialog.id, { role: userDialog.role, permissions_override: permissionsOverride }); userDialog.visible = false; await loadUsers(); ElMessage.success('用户和权限已保存') } finally { userSaving.value = false } }
async function submitReset() { if (!resetDialog.password) return void ElMessage.warning('请输入新密码'); userSaving.value = true; try { await updateUser(resetDialog.id, { password: resetDialog.password }); resetDialog.visible = false; ElMessage.success('密码已重置') } finally { userSaving.value = false } }
async function toggleUser(user: UserType) { const status = user.status === 'active' ? 'disabled' : 'active'; await ElMessageBox.confirm(`确认${status === 'active' ? '启用' : '禁用'}用户“${user.username}”吗？`, '确认操作', { type: 'warning' }); await updateUser(user.id, { status }); await loadUsers(); ElMessage.success('用户状态已更新') }
function roleTag(role: string): 'danger' | 'primary' | 'info' { return role === 'admin' ? 'danger' : role === 'reviewer' ? 'primary' : 'info' }
function formatTime(value?: string) { return formatSystemTime(value, app.systemConfig.timezone, 'YYYY-MM-DD HH:mm:ss') }

const logs = ref<AuditLogItem[]>([])
const logTotal = ref(0)
const logsLoading = ref(false)
const logFilters = reactive({ module: '', q: '', page: 1, size: 20 })
async function loadLogs() { logsLoading.value = true; try { const result = await getAuditLogs(logFilters); logs.value = result.items; logTotal.value = result.total } finally { logsLoading.value = false } }
onMounted(loadAll)
</script>

<style scoped>
.settings-page { min-height: 100%; }
.settings-skeleton { padding: 28px; background: var(--el-bg-color); border-radius: 12px; }
.settings-shell { display: grid; grid-template-columns: 210px minmax(0, 1fr); min-height: calc(100vh - 104px); background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter); border-radius: 12px; overflow: hidden; box-shadow: var(--ccc-card-shadow); }
.settings-nav { padding: 14px 10px; border-right: 1px solid var(--el-border-color-lighter); background: var(--el-fill-color-extra-light); }
.settings-nav-item { width: 100%; height: 44px; display: flex; align-items: center; gap: 11px; border: 0; border-radius: 8px; padding: 0 13px; margin-bottom: 3px; color: var(--el-text-color-regular); background: transparent; font: inherit; cursor: pointer; text-align: left; transition: background-color .18s ease, color .18s ease; }
.settings-nav-item:hover { background: var(--el-fill-color-light); color: var(--ccc-primary); }
.settings-nav-item.active { background: var(--el-color-primary-light-9); color: var(--ccc-primary); font-weight: 600; }
.settings-nav-item .el-icon { font-size: 18px; }
.settings-main { min-width: 0; }
.settings-header { min-height: 84px; display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 18px 24px; border-bottom: 1px solid var(--el-border-color-lighter); position: sticky; top: -20px; z-index: 2; background: var(--el-bg-color); }
.settings-header h2 { margin: 0 0 5px; font-size: 18px; }
.settings-header p, .group-title-row p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.settings-content { padding: 22px 24px 30px; }
.audit-flow { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1px; margin-bottom: 24px; overflow: hidden; border: 1px solid var(--el-border-color-lighter); border-radius: 10px; background: var(--el-border-color-lighter); }
.audit-flow > div { min-width: 0; display: flex; align-items: center; gap: 12px; padding: 15px 16px; background: var(--el-fill-color-extra-light); }
.audit-flow b { width: 28px; height: 28px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 50%; color: var(--el-color-primary); background: var(--el-color-primary-light-9); font-size: 13px; }
.audit-flow span, .audit-flow strong, .audit-flow small { display: block; }
.audit-flow strong { color: var(--el-text-color-primary); font-size: 13px; }
.audit-flow small { margin-top: 3px; color: var(--el-text-color-secondary); line-height: 1.45; }
.setting-group { padding-bottom: 24px; margin-bottom: 24px; border-bottom: 1px solid var(--el-border-color-lighter); }
.setting-group:last-child { border-bottom: 0; margin-bottom: 0; }
.setting-group h3 { margin: 0 0 18px; font-size: 15px; }
.group-title-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.group-title-row h3 { margin-bottom: 5px; }
.form-grid { display: grid; gap: 0 28px; }
.two-columns { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.three-columns { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.full-row { grid-column: 1 / -1; }
.form-grid :deep(.el-select), .form-grid :deep(.el-input-number) { width: 100%; }
.field-help { margin-top: 6px; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.5; }
.provider-note { margin: 4px 0 14px; }
.logo-field { display: flex; align-items: center; gap: 14px; }
.logo-preview { width: 72px; height: 72px; padding: 8px; box-sizing: border-box; display: grid; place-items: center; overflow: hidden; flex: 0 0 auto; border: 1px solid var(--el-border-color); border-radius: 14px; color: var(--ccc-primary); font-weight: 700; background: #fff; box-shadow: 0 8px 22px rgba(24, 52, 84, .1); }
.logo-preview img { width: 100%; height: 100%; object-fit: contain; border-radius: 8px; }
.preference-grid { display: grid; grid-template-columns: 1.2fr .5fr 1.2fr; gap: 30px; }
.preference-block { display: flex; flex-direction: column; gap: 10px; }
.preference-block > label { font-size: 13px; color: var(--el-text-color-regular); }
.section-actions { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
.section-actions > .el-input { width: 260px; }
.permission-matrix { display: grid; grid-template-columns: minmax(260px, 1fr) repeat(3, 120px); margin-top: 18px; border: 1px solid var(--el-border-color-lighter); border-radius: 10px; overflow: hidden; }
.permission-matrix > div { min-height: 58px; display: flex; align-items: center; padding: 10px 16px; border-bottom: 1px solid var(--el-border-color-lighter); }
.permission-head { justify-content: center; font-weight: 600; background: var(--el-fill-color-light); }
.permission-name { flex-direction: column; align-items: flex-start !important; justify-content: center; }
.permission-name span { margin-top: 3px; color: var(--el-text-color-secondary); font-size: 12px; }
.permission-cell { justify-content: center; border-left: 1px solid var(--el-border-color-lighter); }
.user-permission-editor { margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--el-border-color-lighter); }
.permission-mode-row { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 14px; }
.permission-mode-row > div { display: grid; gap: 4px; }
.permission-mode-row span { color: var(--el-text-color-secondary); font-size: 12px; }
.custom-permission-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 16px 0 9px; color: var(--el-text-color-regular); font-size: 12px; }
.custom-permission-summary b { color: var(--el-color-primary); font-size: 15px; }
.custom-permission-summary small { color: var(--el-text-color-secondary); }
.user-permission-options { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.user-permission-options :deep(.el-checkbox) { height: auto; min-height: 64px; align-items: flex-start; margin: 0; padding: 11px 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 9px; background: var(--el-fill-color-extra-light); }
.user-permission-options :deep(.el-checkbox.is-checked) { border-color: var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); }
.user-permission-options :deep(.el-checkbox__input) { margin-top: 2px; }
.user-permission-options :deep(.el-checkbox__label) { min-width: 0; padding-left: 9px; white-space: normal; }
.user-permission-options :deep(.el-checkbox__label span), .user-permission-options :deep(.el-checkbox__label strong), .user-permission-options :deep(.el-checkbox__label small) { display: block; }
.user-permission-options :deep(.el-checkbox__label strong) { color: var(--el-text-color-primary); font-size: 13px; }
.user-permission-options :deep(.el-checkbox__label small) { margin-top: 4px; color: var(--el-text-color-secondary); font-size: 11px; line-height: 1.45; }
.switch-list { display: grid; gap: 0; }
.switch-list > div { min-height: 64px; display: flex; align-items: center; justify-content: space-between; gap: 20px; border-bottom: 1px solid var(--el-border-color-lighter); }
.switch-list > div:last-child { border-bottom: 0; }
.switch-list span { display: flex; flex-direction: column; gap: 4px; }
.switch-list small { color: var(--el-text-color-secondary); }
.public-access-group { border-color: var(--el-border-color); background: var(--el-fill-color-extra-light); transition: border-color .18s ease, background-color .18s ease; }
.public-access-group.is-public { border-color: var(--el-color-warning-light-7); background: var(--el-color-warning-light-9); }
.public-access-row { display: flex; align-items: center; justify-content: space-between; gap: 24px; }
.public-access-row > div { min-width: 0; }
.public-access-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.public-access-title h3 { margin: 0; }
.public-access-row p { margin: 8px 0 0; color: var(--el-text-color-secondary); line-height: 1.6; }
.public-access-row :deep(.el-switch) { flex: 0 0 auto; }
.access-endpoints { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 20px 0 16px; }
.access-endpoints > div { min-width: 0; padding: 12px 14px; border: 1px solid var(--el-border-color-lighter); border-radius: 9px; background: var(--el-bg-color); }
.access-endpoints span, .access-endpoints strong { display: block; }
.access-endpoints span { margin-bottom: 5px; color: var(--el-text-color-secondary); font-size: 12px; }
.access-endpoints strong { overflow-wrap: anywhere; color: var(--el-text-color-primary); font-size: 13px; font-variant-numeric: tabular-nums; }
.inline-actions { display: flex; align-items: center; gap: 12px; }
.inline-actions span { color: var(--el-text-color-secondary); font-size: 12px; }
.repair-status-row { display: grid; grid-template-columns: auto minmax(120px, 1fr) auto minmax(120px, 1fr) auto minmax(150px, 1.2fr); gap: 8px 12px; align-items: center; margin-top: 16px; padding: 13px 14px; background: var(--el-fill-color-extra-light); border-radius: 9px; }
.repair-status-row span { color: var(--el-text-color-secondary); font-size: 12px; }
.repair-status-row strong { color: var(--el-text-color-primary); font-size: 12px; font-weight: 600; overflow-wrap: anywhere; }
.safety-status-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }
.safety-status-list > div { min-width: 0; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; background: var(--el-fill-color-extra-light); border-radius: 9px; }
.safety-status-list strong { color: var(--el-text-color-primary); font-size: 12px; }
.safety-status-list span { color: var(--el-color-success); font-size: 12px; }
.input-suffix { margin-left: 8px; color: var(--el-text-color-secondary); }
.workflow-fields { margin-top: 22px; }
.browser-sync-form { margin-top: 20px; }
.pairing-code-row { width: 100%; min-height: 40px; display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.pairing-code-row strong { letter-spacing: .18em; color: var(--el-color-primary); font-size: 24px; font-variant-numeric: tabular-nums; }
.log-filters { display: flex; gap: 10px; }
.log-filters .el-select { width: 150px; }
.log-filters .el-input { width: 240px; }
.el-pagination { justify-content: flex-end; margin-top: 18px; }
@media (max-width: 1100px) { .settings-shell { grid-template-columns: 176px minmax(0, 1fr); } .settings-nav-item { padding: 0 10px; } .three-columns { grid-template-columns: repeat(2, minmax(0, 1fr)); } .preference-grid, .safety-status-list { grid-template-columns: 1fr; } .permission-matrix { grid-template-columns: minmax(220px, 1fr) repeat(3, 90px); } .repair-status-row { grid-template-columns: auto minmax(0, 1fr); } }
@media (max-width: 760px) { .settings-shell { display: block; } .settings-nav { display: flex; overflow-x: auto; gap: 4px; border-right: 0; border-bottom: 1px solid var(--el-border-color-lighter); } .settings-nav-item { width: auto; flex: 0 0 auto; margin: 0; } .settings-header { top: -14px; padding: 16px; } .settings-header p { display: none; } .settings-content { padding: 18px 16px 24px; } .audit-flow { grid-template-columns: 1fr; } .two-columns, .three-columns, .user-permission-options { grid-template-columns: 1fr; } .permission-matrix { min-width: 620px; } .section-actions, .log-filters, .permission-mode-row, .inline-actions { align-items: flex-start; flex-wrap: wrap; } }
</style>
