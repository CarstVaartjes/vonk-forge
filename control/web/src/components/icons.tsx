import type {SVGProps} from "react";

type IconProps = Omit<SVGProps<SVGSVGElement>, "children">;

function Icon({children, ...props}: IconProps & {children: React.ReactNode}) {
  return <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...props}>{children}</svg>;
}

export function FleetIcon(props: IconProps) {
  return <Icon {...props}><rect x="4" y="4" width="16" height="6" rx="2"/><rect x="4" y="14" width="16" height="6" rx="2"/><path d="M8 7h.01M8 17h.01M12 7h5M12 17h5"/></Icon>;
}

export function LibraryIcon(props: IconProps) {
  return <Icon {...props}><path d="M5 4h5v16H5zM14 4h5v16h-5z"/><path d="M7.5 8h.01M16.5 8h.01M7.5 16h.01M16.5 16h.01"/></Icon>;
}

export function ActivityIcon(props: IconProps) {
  return <Icon {...props}><path d="M3 12h4l2.2-6 4.1 12 2.2-6H21"/></Icon>;
}

export function SystemIcon(props: IconProps) {
  return <Icon {...props}><circle cx="12" cy="12" r="3"/><path d="M19 13.5v-3l-2-.7-.7-1.7.9-1.9-2.1-2.1-1.9.9-1.7-.7L10.5 2h-3l-.7 2-1.7.7-1.9-.9-2.1 2.1.9 1.9-.7 1.7-2 .7v3l2 .7.7 1.7-.9 1.9 2.1 2.1 1.9-.9 1.7.7.7 2h3l.7-2 1.7-.7 1.9.9 2.1-2.1-.9-1.9.7-1.7z" transform="translate(3 -1.5) scale(.75)"/></Icon>;
}

export function MenuIcon(props: IconProps) {
  return <Icon {...props}><path d="M4 7h16M4 12h16M4 17h16"/></Icon>;
}

export function CloseIcon(props: IconProps) {
  return <Icon {...props}><path d="m6 6 12 12M18 6 6 18"/></Icon>;
}

export function ChevronIcon(props: IconProps) {
  return <Icon {...props}><path d="m9 6 6 6-6 6"/></Icon>;
}
