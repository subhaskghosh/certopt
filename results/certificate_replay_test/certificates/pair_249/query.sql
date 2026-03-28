SELECT
  p.email
FROM PERSON AS P
GROUP BY
  email
HAVING
  COUNT(email) > 1