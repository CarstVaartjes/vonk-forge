import type {components} from "../api/generated";

export type RecipeOption = components["schemas"]["RecipeOption"];
export type OptionChoices = Record<string, string>;

/** Every option's value: the stored choice when the recipe still offers it, else the recipe default. */
export function effectiveChoices(options: RecipeOption[], stored: OptionChoices = {}): OptionChoices {
  return Object.fromEntries(options.map(option => {
    const kept = option.choices.find(choice => choice.value === stored[option.name]);
    const fallback = option.choices.find(choice => choice.default) ?? option.choices[0];
    return [option.name, (kept ?? fallback).value];
  }));
}

export function RecipeOptionSelects({options, value, onChange, idPrefix}: {options: RecipeOption[]; value: OptionChoices; onChange(next: OptionChoices): void; idPrefix: string}) {
  if (options.length === 0) return null;
  const current = effectiveChoices(options, value);
  return <fieldset className="recipe-options"><legend>Recipe options</legend>
    {options.map(option => {
      const id = `${idPrefix}-${option.name}`;
      const chosen = option.choices.find(choice => choice.value === current[option.name]);
      return <div key={option.name} className="recipe-option">
        <label htmlFor={id}><span>{option.label}</span></label>
        <select id={id} value={current[option.name]} onChange={event => onChange({...current, [option.name]: event.target.value})}>
          {option.choices.map(choice => <option key={choice.value} value={choice.value}>{choice.label}{choice.default ? " (default)" : ""}</option>)}
        </select>
        <small>{option.help}{chosen ? ` ${chosen.help}` : ""}</small>
      </div>;
    })}
  </fieldset>;
}
