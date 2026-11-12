import type { LibraryViewRecipeDetail as RecipeDetail } from "../api/types";
import { recipePublicUrl, webAddress } from "../lib/public-site";

function Address({ value, children }: { value: string | null | undefined; children: string }) {
  const href = webAddress(value);
  return href ? (
    <a href={href} target="_blank" rel="noreferrer">
      {children}
    </a>
  ) : (
    <>{value || children}</>
  );
}

/**
 * Where the recipe comes from: the creator's page on the public site, each model's licence and the
 * recipe source. Licences and regional limits are information for the operator; nothing here gates
 * a download, install or load.
 */
export function LibraryRecipeLinks({ detail }: { detail: RecipeDetail }) {
  const source = detail.definition.provenance.source_reference;
  const licences = detail.model_documents.flatMap((item) =>
    item.model_display
      ? [
          {
            key: `${item.model_document.identity.publisher}/${item.model_document.identity.slug}`,
            title: item.model_display.model_title,
            license: item.model_display.license,
          },
        ]
      : [],
  );
  return (
    <section className="library-section" aria-label="Creator, licence and source">
      <header>
        <h3>Creator, licence and source</h3>
      </header>
      <ul className="library-recipe-links">
        <li>
          <a href={recipePublicUrl(detail.recipe)} target="_blank" rel="noreferrer">
            Creator and details on vonkforge.ai
          </a>
        </li>
        {source && (
          <li>
            Recipe source: <Address value={source}>{source}</Address>
          </li>
        )}
        {licences.map(({ key, title, license }) => (
          <li key={key}>
            {title} licence: {license.spdx} (<Address value={license.url}>terms</Address>)
            {license.territorial_restrictions && (
              <>
                . Regional limits ({license.territorial_restrictions.denied_jurisdictions.join(", ")}):{" "}
                {license.territorial_restrictions.notice}
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
