import type {ReactNode, Ref} from "react";

/** Every page opens the same way: title, one-line description, primary action top right. */
export function PageHeader({title, description, actions, headingRef, children}: {title: string; description: string; actions?: ReactNode; headingRef?: Ref<HTMLHeadingElement>; children?: ReactNode}) {
  return <header className="page-header">
    <div className="page-header-title"><h1 ref={headingRef} tabIndex={-1}>{title}</h1><p>{description}</p>{children}</div>
    {actions && <div className="page-header-actions">{actions}</div>}
  </header>;
}
