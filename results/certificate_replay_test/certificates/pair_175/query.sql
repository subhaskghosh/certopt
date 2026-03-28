SELECT
  p.email
FROM PERSON AS P
GROUP BY
  p.email
HAVING
  COUNT(p.email) > 1