import type { ReactNode } from 'react'

interface DrawerProps {
  title: string
  onClose: () => void
  footer?: ReactNode
  children: ReactNode
}

/** 右侧滑出抽屉：用于预演名单明细、待处理明细与留痕回看。 */
export default function Drawer({ title, onClose, footer, children }: DrawerProps) {
  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <section className="drawer" role="dialog" aria-label={title}>
        <header className="drawer-head">
          <h3>{title}</h3>
          <button className="btn ghost" type="button" onClick={onClose}>关闭</button>
        </header>
        <div className="drawer-body">{children}</div>
        {footer && <footer className="drawer-foot">{footer}</footer>}
      </section>
    </>
  )
}
