SELECT
  emailcount.email
FROM (
  SELECT
    EMAIL,
    COUNT(EMAIL) AS COUNT
  FROM PERSON
  GROUP BY
    EMAIL
) AS EMAILCOUNT
WHERE
  emailcount.count > 1